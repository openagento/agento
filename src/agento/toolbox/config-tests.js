// The toolbox side of the "Test connection" feature.
//
// The probe runs HERE because this is the process that already holds the
// secrets (CLAUDE.md -> Security). The framework asks one question over HTTP —
// "test the field at this path" — and gets back four possible answers. Nothing
// in the framework decrypts anything to make that happen.
//
// A declaration lives on the field, in the module's own system.json:
//
//   "smtp_password": {
//     "type": "obscure",
//     "tester": { "kind": "smtp", "host": "{core/smtp_host}", "pass": "{core/smtp_pass}" }
//   }
//
// A bare string is sugar for a named probe the module exports from its
// toolbox/ directory:  "tester": "graph_credentials".
import fs from 'node:fs';
import path from 'node:path';
import { z } from 'zod';

import {
  discoverToolboxFiles,
  loadStrictScopedOverrides,
  readConfigDefaults,
  redactSecrets,
  resolveModuleFieldStrict,
  scanModules,
} from './config-loader.js';
import * as smtpProbe from './probes/smtp.js';
import * as httpProbe from './probes/http.js';

export const CONFIG_TEST_TIMEOUT_MS = 15_000;

const BUILTIN_PROBES = { smtp: smtpProbe, http: httpProbe };

// `{module/field}` — a config path and nothing else, so a literal `{}` or
// `{some prose}` in a header value is left alone.
const PLACEHOLDER = /\{([a-z0-9_]+(?:\/[a-z0-9_/-]+)+)\}/gi;

const CODE_SHAPE = /^[A-Z0-9_]{1,40}$/;

// The whole four-state vocabulary and nothing else. `error` means "could not
// check"; anything a probe returns outside this set is treated the same way.
const STATUSES = ['ok', 'fail', 'not_configured', 'error'];

// Every field a probe returns, normalized. `.catch()` on the tolerant fields
// keeps a sloppy-but-readable result usable; `status` deliberately has none.
const RESULT_SCHEMA = z.object({
  status: z.enum(STATUSES),
  code: z.string().catch(''),
  detail: z.string().catch(''),
  ms: z.number().finite().nonnegative().catch(NaN),
});

function err(code, detail) {
  return { status: 'error', code, detail };
}

/** A field's `tester` value, normalized. `null` when there is no declaration. */
export function normalizeTester(raw) {
  if (typeof raw === 'string' && raw.trim()) {
    return { kind: 'toolbox', name: raw.trim() };
  }
  if (raw && typeof raw === 'object' && !Array.isArray(raw) && typeof raw.kind === 'string') {
    return { ...raw };
  }
  return null;
}

function readSystem(modulePath) {
  const systemPath = path.join(modulePath, 'system.json');
  if (!fs.existsSync(systemPath)) return null;
  try {
    const parsed = JSON.parse(fs.readFileSync(systemPath, 'utf-8'));
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
    return parsed;
  } catch {
    return null;
  }
}

/**
 * The declaration for one config path.
 *
 * A path is `<module>/<field>` where the field itself may contain slashes
 * (`app_monitor/alerts/smtp_password`), so the split is on the FIRST slash only.
 */
export function findDeclaration(configPath, modules = scanModules()) {
  const raw = String(configPath || '');
  const cut = raw.indexOf('/');
  if (cut <= 0 || cut === raw.length - 1) return null;
  const moduleName = raw.slice(0, cut);
  const field = raw.slice(cut + 1);

  const mod = modules.find((m) => m.name === moduleName);
  if (!mod) return null;
  const system = readSystem(mod._path);
  if (!system) return null;
  const schema = system[field];
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) return null;

  const spec = normalizeTester(schema.tester);
  if (!spec) return null;
  const { kind, ...rest } = spec;
  return { module: moduleName, moduleDir: mod._path, field, kind, spec: rest, system };
}

/** Every distinct `{module/field}` referenced anywhere in a declaration. */
export function placeholdersOf(spec) {
  const found = new Set();
  const walk = (node) => {
    if (typeof node === 'string') {
      for (const m of node.matchAll(PLACEHOLDER)) found.add(m[1]);
      return;
    }
    if (Array.isArray(node)) { node.forEach(walk); return; }
    if (node && typeof node === 'object') { Object.values(node).forEach(walk); }
  };
  walk(spec);
  return [...found];
}

/** Substitute resolved values into a declaration. Non-strings pass through. */
export function interpolate(spec, values) {
  const walk = (node) => {
    if (typeof node === 'string') {
      return node.replace(PLACEHOLDER, (whole, p) => {
        if (!(p in values)) return whole;
        const v = values[p];
        return v === null || v === undefined ? '' : String(v);
      });
    }
    if (Array.isArray(node)) return node.map(walk);
    if (node && typeof node === 'object') {
      return Object.fromEntries(Object.entries(node).map(([k, v]) => [k, walk(v)]));
    }
    return node;
  };
  return walk(spec);
}


/**
 * Named probes a module exports from its toolbox/ directory:
 *
 *   export const configTests = [{ name, paths, run }]
 *
 * `run({ config, signal, timeoutMs })` gets ONLY the paths it declared,
 * keyed by field name, so an existing module helper takes it unchanged.
 * `signal` is aborted when `timeoutMs` runs out — a probe that reaches the
 * network must pass it on, or its request outlives the answer.
 */
export async function registerConfigTests(modules = scanModules()) {
  const registry = new Map();
  for (const mod of modules) {
    for (const file of discoverToolboxFiles(mod._path)) {
      let imported;
      try {
        imported = await import(file);
      } catch {
        continue;   // a broken module file must not take the route down
      }
      // The CONTAINER is input too, not only its members. `for...of` over a
      // non-iterable export — `configTests = {name, run}`, one declaration
      // written without the array — threw a TypeError out of this function,
      // which left the registry an empty Map: every named probe in every module
      // then answered UNKNOWN_TESTER because ONE module exported the wrong
      // shape. A non-array export is dropped with its file instead.
      const declared = CONTAINER_SCHEMA.safeParse(imported.configTests);
      if (!declared.success) continue;
      for (const raw of declared.data) {
        const entry = DECLARATION_SCHEMA.safeParse(raw);
        if (!entry.success) continue;   // a malformed declaration registers nothing
        const { name, paths, run } = entry.data;
        if (registry.has(name)) {
          // Fail CLOSED, not first-wins. `module:validate` reads manifests and
          // cannot see a JavaScript export, so a clash inside one module used to
          // be resolved by readdir order — the probe that ran depended on a
          // filename. A conflicted name now answers DUPLICATE_TESTER, which is a
          // diagnosis instead of a coin flip.
          registry.set(name, { conflict: true });
          continue;
        }
        registry.set(name, { module: mod.name, paths, run });
      }
    }
  }
  return registry;
}

const CONFIG_PATH_SHAPE = /^[a-z0-9_]+\/[a-z0-9_/-]+$/i;

// What a module may put in `configTests`. Validated at REGISTRATION, because
// this is module metadata crossing into the framework: a `paths` member that is
// not a string reached `p.startsWith` inside path resolution and threw a
// TypeError out of the route, which breaks the four-state contract with a stack
// trace instead of answering `error`.
const CONTAINER_SCHEMA = z.array(z.unknown());

const DECLARATION_SCHEMA = z.object({
  // Trimmed, so a padded name registers under the identity a system.json
  // `tester` string will actually look up, and a whitespace-only name is empty.
  name: z.string().trim().min(1),
  paths: z.array(z.string().regex(CONFIG_PATH_SHAPE)).default([]),
  run: z.custom((v) => typeof v === 'function'),
});

// A positive decimal integer and nothing else. `parseInt` is the wrong tool here:
// `parseInt('1.5', 10)` is 1 and `parseInt('7abc', 10)` is 7, so a scope the caller
// never asked for would be silently accepted.
const AGENT_VIEW_ID_SHAPE = /^[1-9][0-9]*$/;

const REQUEST_SCHEMA = z.object({
  path: z.string().regex(CONFIG_PATH_SHAPE),
  agent_view_id: z.union([z.string(), z.number()])
    .transform((v) => String(v))
    .refine((v) => AGENT_VIEW_ID_SHAPE.test(v) && Number.isSafeInteger(Number(v)))
    .transform(Number)
    .optional(),
});

/**
 * Validate one `/config-test` request: path shape and scope, nothing else.
 *
 * Pure and exported so the route's guards are covered by a unit test rather than
 * only reachable through a live server. Deliberately does NOT rate-limit: the
 * limit has to be keyed on the CREDENTIAL, and which credential a path names is
 * only known after the declaration is resolved — see `ProbeLimiter`.
 */
export function parseConfigTestRequest(query = {}) {
  const parsed = REQUEST_SCHEMA.safeParse(query);
  if (!parsed.success) {
    // Which field failed decides the code, so the caller learns whether it named
    // a bad path or a bad scope — the body is the only channel this route has.
    const badPath = parsed.error.issues.some((i) => i.path[0] === 'path');
    const raw = typeof query.path === 'string' ? query.path : '';
    return badPath
      ? { error: err('BAD_PATH', 'path must be <module>/<field>'), path: raw }
      : { error: err('BAD_SCOPE', 'agent_view_id must be a positive integer'), path: raw };
  }
  return { configPath: parsed.data.path, agentViewId: parsed.data.agent_view_id ?? null };
}

/**
 * Bounded per-credential probe limiter.
 *
 * Three things this must get right, each of which a per-path Map got wrong:
 *
 *  1. **Key on the credential, not the field path.** One credential is reached
 *     through several field paths — that is the whole reason `enumerate_test_groups`
 *     exists on the Python side. A per-path limit lets a caller rotate through the
 *     six Outlook Graph fields and drive six times the login rate at one account.
 *     The key is the declaration group (module + normalized tester) plus the scope,
 *     so aliases collapse and two different agent_views stay independent.
 *  2. **Bounded.** `sessions` and `McpSessionRegistry` are the two module-scope
 *     Maps that already exist in `server.js`, and both are bounded (delete on
 *     close, idle sweep). An unbounded Map fed by a request parameter grows for as
 *     long as the process lives. This one prunes expired entries on every write and
 *     hard-caps at `maxEntries`, evicting the oldest — so a caller inventing keys
 *     costs memory that is O(cap), not O(requests).
 *  3. **Stamp at start, not at completion.** `take()` records the timestamp before
 *     the probe runs, so N requests arriving at once see the stamp the first one
 *     wrote and only one proceeds. That is the single-flight property; it needs no
 *     separate in-flight set and no extra error code.
 *
 * Only ever constructed once, at module load, and never rebound per session — the
 * one shape the toolbox session-isolation rule permits at module scope.
 */
export class ProbeLimiter {
  constructor({ cooldownMs = 3_000, maxEntries = 512, clock = () => Date.now() } = {}) {
    this._cooldownMs = cooldownMs;
    this._maxEntries = maxEntries;
    this._clock = clock;
    this._last = new Map();
  }

  /** `{ ok: true }`, or `{ ok: false, sinceMs }` when the same credential is still cooling down. */
  take(key) {
    const now = this._clock();
    const previous = this._last.get(key);
    if (previous !== undefined && now - previous < this._cooldownMs) {
      return { ok: false, sinceMs: now - previous };
    }
    for (const [k, at] of this._last) {
      if (now - at >= this._cooldownMs) this._last.delete(k);
    }
    while (this._last.size >= this._maxEntries) {
      // Map preserves insertion order, so the first key is the oldest write.
      this._last.delete(this._last.keys().next().value);
    }
    this._last.set(key, now);
    return { ok: true };
  }

  get size() {
    return this._last.size;
  }
}

/**
 * The limiter key for one resolved declaration at one scope.
 *
 * `module` + the normalized tester JSON is the same key `enumerate_test_groups`
 * uses in `manifest.py`, so the two sides group identically: change one and the
 * parity test in Task 2 fails.
 */
export function limiterKey(decl, agentViewId) {
  return `${decl.module}\x00${decl.kind}\x00${stableStringify(decl.spec)}\x00${agentViewId ?? 'default'}`;
}

/**
 * Key-order-independent JSON, all the way down.
 *
 * NOT `JSON.stringify(o, Object.keys(o).sort())`: a replacer ARRAY filters keys at
 * every depth, so any key that appears only inside a nested object — `headers`'
 * contents, for instance — is dropped from the output. Two http testers differing
 * only in a nested field would then produce the same key and share one cooldown.
 */
function stableStringify(node) {
  if (Array.isArray(node)) return `[${node.map(stableStringify).join(',')}]`;
  if (node && typeof node === 'object') {
    const body = Object.keys(node).sort()
      .map((k) => `${JSON.stringify(k)}:${stableStringify(node[k])}`)
      .join(',');
    return `{${body}}`;
  }
  return JSON.stringify(node) ?? 'null';
}

function fieldOf(configPath) {
  const cut = configPath.indexOf('/');
  return cut < 0 ? configPath : configPath.slice(cut + 1);
}

async function resolvePaths(paths, moduleName, moduleDir, overrides, system = {}) {
  const defaults = readConfigDefaults(moduleDir);
  const values = {};
  const missing = [];
  const secrets = [];
  for (const p of paths) {
    if (!p.startsWith(`${moduleName}/`)) return { foreign: p };
    const field = fieldOf(p);
    const { value, state } = resolveModuleFieldStrict(moduleName, field, defaults, overrides);
    if (state === 'undecryptable') return { undecryptable: field };
    if (state === 'unset' || value === null || value === '') missing.push(field);
    values[p] = value;
    // The redaction set is exactly this declaration's own obscure values. A
    // probe cannot quote anything else: a named probe receives `config` built
    // from these paths and no DB handle, and a builtin probe receives only the
    // interpolated spec.
    if (system[field] && system[field].type === 'obscure' && value) secrets.push(String(value));
  }
  return { values, missing, secrets };
}

// Races the probe against the clock AND cancels it: `controller.abort()` is what
// makes a named probe's in-flight token request stop. Without it the login
// attempt continued after the caller already had its PROBE_TIMEOUT answer, so a
// probe that hung kept authenticating against the remote account off-books —
// which is exactly what the per-credential cooldown exists to bound.
// A private Symbol, not a `{__timedOut: true}` marker: the marker was read off
// the raced value BEFORE the result parser ran, so a probe returning that field
// could label its own answer PROBE_TIMEOUT. A Symbol compared by identity cannot
// be produced by a module.
const TIMED_OUT = Symbol('config-test-timeout');

function withTimeout(start, timeoutMs) {
  const controller = new globalThis.AbortController();
  let timer;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => {
      controller.abort();
      resolve(TIMED_OUT);
    }, timeoutMs);
  });
  return Promise.race([start(controller.signal), timeout])
    .finally(() => clearTimeout(timer));
}

/**
 * Run the test declared on one config field.
 *
 * Never throws and never returns a shape the caller has to guess at: always
 * `{status, code, detail, ms}` with `status` one of ok / fail / not_configured /
 * error, and `code` matching ^[A-Z0-9_]{1,40}$ so it can be printed unsanitized.
 */
export async function runConfigTest({ path: configPath, agentViewId = null }, deps = {}) {
  const modules = deps.modules || scanModules();
  const probes = deps.probes || BUILTIN_PROBES;
  const namedTests = deps.namedTests || new Map();
  const timeoutMs = deps.timeoutMs || CONFIG_TEST_TIMEOUT_MS;
  // The STRICT loader, not the lenient one every MCP session uses: a config test
  // that silently fell back to global config would report a verdict about a
  // credential the caller never named. See `loadStrictScopedOverrides`.
  const loadOverrides = deps.loadOverrides || loadStrictScopedOverrides;

  const decl = findDeclaration(configPath, modules);
  if (!decl) {
    return err('NO_TESTER', `'${configPath}' declares no tester in its module's system.json`);
  }
  if (decl.kind === 'local') {
    return err(
      'NOT_A_TOOLBOX_TEST',
      `'${configPath}' declares a local tester, which runs in the framework`,
    );
  }

  // Own-module paths only, checked before anything dispatches on kind and before
  // the cooldown is spent: a declaration reaching into another module's config can
  // never run, whatever its kind, and must say so.
  const foreign = placeholdersOf(decl.spec).find((p) => !p.startsWith(`${decl.module}/`));
  if (foreign) {
    return err(
      'FOREIGN_PATH',
      `'${foreign}' does not belong to module '${decl.module}' — a tester may only `
      + 'read its own module\'s config',
    );
  }

  let overrides;
  try {
    overrides = await loadOverrides(agentViewId);
  } catch (e) {
    return err('CONFIG_UNAVAILABLE', `could not read stored config (${(e && e.name) || 'Error'})`);
  }

  // `secrets` is filled once the declaration's own paths are resolved, below.
  // Decrypting EVERY module's obscure fields to scrub one probe's message is the
  // `resolve_all()` shape CLAUDE.md forbids for a value used to build a probe —
  // it widens exposure per request and buys nothing, since a probe only ever
  // sees the paths its own declaration named.
  let secrets = [];
  const clean = (text) => redactSecrets(String(text ?? ''), secrets).replace(/\s+/g, ' ').trim();
  // A named probe is third-party module code that was handed obscure values, so
  // EVERY field of its result is input: `status` decides how the framework
  // renders it, `code` is printed unsanitized by contract and copied into the
  // REST log, and `ms` is returned as-is. `status` has no `.catch` on purpose —
  // an unreadable status is the one thing that cannot be normalized, so it fails
  // the parse and becomes BAD_RESULT with a FIXED message (interpolating the
  // offending value into the detail is how the leak this guards against gets
  // reintroduced).
  const finish = (r, ms) => {
    const parsed = RESULT_SCHEMA.safeParse(r);
    if (!parsed.success) {
      return {
        status: 'error', code: 'BAD_RESULT',
        detail: 'the probe returned a result this side cannot read',
        ms, path: configPath,
      };
    }
    const v = parsed.data;
    // A code is printed unredacted, so it is only allowed to BE a code: shaped,
    // and unchanged by redaction. `clean()` differs from its input exactly when
    // the text carried one of this declaration's secrets — a secret shaped like
    // `SECRETPASS` would otherwise pass the regex and reach the log.
    const shaped = CODE_SHAPE.test(v.code) && clean(v.code) === v.code;
    const code = shaped ? v.code : (v.status === 'ok' ? 'OK' : 'UNSPECIFIED');
    return {
      status: v.status,
      code,
      detail: clean(v.detail),
      ms: Number.isFinite(v.ms) ? v.ms : ms,
      path: configPath,
    };
  };

  // `prepare(values)` answers either `{notConfigured}` — a verdict this side
  // reached on its own — or `{run}`, the one call that reaches the remote
  // account. Splitting it that way is what lets the cooldown be spent on the
  // attempt and nothing else (see below).
  let prepare;
  let paths;
  if (decl.kind === 'toolbox') {
    const named = namedTests.get(decl.spec.name);
    if (!named) {
      return err('UNKNOWN_TESTER', `no probe named '${decl.spec.name}' is registered`);
    }
    if (named.conflict) {
      return err(
        'DUPLICATE_TESTER',
        `a probe named '${decl.spec.name}' is registered more than once`,
      );
    }
    if (named.module !== decl.module) {
      return err(
        'FOREIGN_TESTER',
        `'${decl.spec.name}' is registered by module '${named.module}', not '${decl.module}'`,
      );
    }
    paths = named.paths;
    prepare = (values) => ({
      run: (ctx) => named.run({
        config: Object.fromEntries(paths.map((p) => [fieldOf(p), values[p]])),
        ...ctx,
      }),
    });
  } else {
    const probe = probes[decl.kind];
    if (!probe) return err('UNKNOWN_KIND', `unknown tester kind '${decl.kind}'`);
    paths = placeholdersOf(decl.spec);
    prepare = (values) => {
      const spec = interpolate(decl.spec, values);
      for (const field of probe.required || []) {
        if (spec[field] === undefined || spec[field] === null || spec[field] === '') {
          // Name the CONFIG field that fed it, not the probe's own spec key:
          // "'host' is empty" sends the reader looking for a field called `host`,
          // which does not exist in any system.json.
          const from = placeholdersOf(decl.spec[field]).map(fieldOf);
          const which = from.length ? from.join(', ') : field;
          return {
            notConfigured: {
              status: 'not_configured', code: 'NOT_SET',
              detail: `'${which}' is empty — nothing to test`,
            },
          };
        }
      }
      return { run: (ctx) => probe.probe(spec, ctx) };
    };
  }

  const resolved = await resolvePaths(
    paths, decl.module, decl.moduleDir, overrides, decl.system || {},
  );
  secrets = resolved.secrets || [];
  if (resolved.foreign) {
    return err(
      'FOREIGN_PATH',
      `'${resolved.foreign}' does not belong to module '${decl.module}' — a tester may only `
      + 'read its own module\'s config',
    );
  }
  if (resolved.undecryptable) {
    // NOT not_configured: the value IS stored, and calling it absent is the
    // misdiagnosis this whole feature exists to end.
    return err(
      'DECRYPT_FAILED',
      `'${resolved.undecryptable}' is stored but could not be decrypted — check AGENTO_ENCRYPTION_KEY`,
    );
  }
  if (resolved.missing.length && decl.kind === 'toolbox') {
    // A named probe decides for itself what "configured" means (Outlook needs a
    // secret OR a certificate), so an empty field is only reported here when the
    // probe declared nothing at all to run with.
    if (resolved.missing.length === paths.length) {
      return {
        status: 'not_configured', code: 'NOT_SET',
        detail: 'no credentials are configured', ms: 0, path: configPath,
      };
    }
  }

  const plan = prepare(resolved.values);
  if (plan.notConfigured) {
    return { ...plan.notConfigured, ms: 0, path: configPath };
  }

  // Rate-limit HERE — the last line before the only call that can reach the
  // remote account, and after every verdict this side reaches on its own.
  //
  // It used to be taken as soon as the declaration resolved, which spent the
  // cooldown on answers that authenticated against nothing: DUPLICATE_TESTER,
  // CONFIG_UNAVAILABLE, DECRYPT_FAILED, NOT_SET. The operator then saw the real
  // diagnosis once and COOLDOWN for the next three seconds — the cooldown
  // masking the fault it was never protecting against.
  //
  // Still per credential and never per caller: the resource being protected is
  // the remote account (an account lockout), not this process. Everything above
  // this line is local — a config read and a decrypt — so leaving it uncapped
  // costs the toolbox nothing a caller on the internal network could not already
  // spend through `/sse`.
  if (deps.limiter) {
    const taken = deps.limiter.take(limiterKey(decl, agentViewId));
    if (!taken.ok) {
      return err(
        'COOLDOWN',
        `this credential was tested ${taken.sinceMs} ms ago — wait a moment`,
      );
    }
  }

  const started = Date.now();
  let raw;
  try {
    raw = await withTimeout(
      (signal) => plan.run({ timeoutMs, signal, deps: deps.probeDeps || {} }),
      timeoutMs,
    );
  } catch (e) {
    return finish(
      { status: 'error', code: 'PROBE_CRASHED', detail: (e && e.message) || 'probe threw' },
      Date.now() - started,
    );
  }
  if (raw === TIMED_OUT) {
    return finish(
      { status: 'error', code: 'PROBE_TIMEOUT', detail: `no answer within ${timeoutMs} ms` },
      Date.now() - started,
    );
  }
  // Anything else — `null`, a string, an object without a status, an object
  // claiming to be the timeout — goes through `finish`, which reports
  // BAD_RESULT. Folding it into PROBE_TIMEOUT told the operator the probe hung
  // when it had in fact answered, badly.
  return finish(raw, Date.now() - started);
}
