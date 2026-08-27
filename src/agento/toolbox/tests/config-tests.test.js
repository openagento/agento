import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  ProbeLimiter,
  registerConfigTests,
  findDeclaration,
  interpolate,
  limiterKey,
  normalizeTester,
  parseConfigTestRequest,
  placeholdersOf,
  runConfigTest,
} from '../config-tests.js';

function moduleDir(name, system, extra = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `cfgtest-${name}-`));
  fs.writeFileSync(path.join(dir, 'module.json'), JSON.stringify({ name, version: '0.1.0' }));
  fs.writeFileSync(path.join(dir, 'system.json'), JSON.stringify(system));
  if (extra.config) fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify(extra.config));
  return { name, _path: dir };
}

describe('parseConfigTestRequest', () => {
  it('accepts a nested path and a positive agent_view_id', () => {
    const out = parseConfigTestRequest(
      { path: 'app_monitor/alerts/smtp_password', agent_view_id: '7' },
    );
    expect(out).toEqual({ configPath: 'app_monitor/alerts/smtp_password', agentViewId: 7 });
  });

  it('omits the scope when no agent_view_id is given', () => {
    expect(parseConfigTestRequest({ path: 'core/smtp_pass' }))
      .toEqual({ configPath: 'core/smtp_pass', agentViewId: null });
  });

  it.each([
    ['..%2Fetc', 'BAD_PATH'],
    ['nomodule', 'BAD_PATH'],
    ['', 'BAD_PATH'],
  ])('refuses %s', (raw, code) => {
    expect(parseConfigTestRequest({ path: raw }).error.code).toBe(code);
  });

  it.each(['abc', '0', '-1', '1.5', '7abc', ' 7', '+7', '1e3', '07'])(
    'refuses agent_view_id=%s', (raw) => {
      // NOT silently null, and NOT `parseInt`: falling back to the default scope —
      // or reading '1.5' as 1 — answers a question about credentials the caller
      // never named.
      const out = parseConfigTestRequest({ path: 'core/smtp_pass', agent_view_id: raw });
      expect(out.error.code).toBe('BAD_SCOPE');
    },
  );

  it('does not rate-limit — that is keyed on the credential, not the path', () => {
    expect(parseConfigTestRequest({ path: 'core/smtp_pass' }).configPath).toBe('core/smtp_pass');
    expect(parseConfigTestRequest({ path: 'core/smtp_pass' }).configPath).toBe('core/smtp_pass');
  });
});

describe('ProbeLimiter', () => {
  const limiter = (now) => new ProbeLimiter({ cooldownMs: 3_000, clock: () => now.at });

  it('lets the first probe through and refuses the second inside the window', () => {
    const now = { at: 1_000_000 };
    const l = limiter(now);
    expect(l.take('k').ok).toBe(true);
    now.at += 500;
    expect(l.take('k')).toEqual({ ok: false, sinceMs: 500 });
    now.at += 2_600;
    expect(l.take('k').ok).toBe(true);
  });

  it('keeps independent keys independent', () => {
    const now = { at: 1_000_000 };
    const l = limiter(now);
    expect(l.take('a').ok).toBe(true);
    expect(l.take('b').ok).toBe(true);
  });

  it('stamps at start, so concurrent takes let exactly one through', () => {
    // The single-flight property: N simultaneous requests all see the stamp the
    // first one wrote, because take() records before the probe runs.
    const now = { at: 1_000_000 };
    const l = limiter(now);
    const results = [l.take('k'), l.take('k'), l.take('k')];
    expect(results.filter((r) => r.ok)).toHaveLength(1);
  });

  it('prunes expired entries and never exceeds the cap', () => {
    const now = { at: 1_000_000 };
    const l = new ProbeLimiter({ cooldownMs: 3_000, maxEntries: 8, clock: () => now.at });
    for (let i = 0; i < 200; i += 1) l.take(`k${i}`);
    expect(l.size).toBeLessThanOrEqual(8);
    now.at += 4_000;
    l.take('fresh');
    expect(l.size).toBe(1);      // everything older than the window is gone
  });
});

describe('limiterKey', () => {
  const decl = (module, spec, kind = 'smtp') => ({ module, kind, spec });

  it('collapses two fields that declare the same tester', () => {
    const spec = { kind: 'smtp', host: '{m/host}' };
    expect(limiterKey(decl('m', spec), null))
      .toBe(limiterKey(decl('m', { host: '{m/host}', kind: 'smtp' }), null));
  });

  it('ignores key order at every depth, and separates nested differences', () => {
    // A replacer-array stringify drops keys that appear only inside a nested
    // object, collapsing two distinct credentials onto one cooldown.
    const a = { kind: 'http', url: '{m/u}', headers: { 'X-Tenant': 'a', Accept: 'json' } };
    const b = { url: '{m/u}', kind: 'http', headers: { Accept: 'json', 'X-Tenant': 'a' } };
    const c = { kind: 'http', url: '{m/u}', headers: { Accept: 'json', 'X-Tenant': 'b' } };
    expect(limiterKey(decl('m', a, 'http'), null)).toBe(limiterKey(decl('m', b, 'http'), null));
    expect(limiterKey(decl('m', a, 'http'), null)).not.toBe(limiterKey(decl('m', c, 'http'), null));
  });

  it('separates two different credentials of the same kind', () => {
    // Otherwise testing the user token reports on the admin one.
    expect(limiterKey(decl('m', { kind: 'http', url: '{m/u1}' }, 'http'), null))
      .not.toBe(limiterKey(decl('m', { kind: 'http', url: '{m/u2}' }, 'http'), null));
  });

  it('separates scopes, so one agent_view cannot exhaust another', () => {
    const spec = { kind: 'smtp', host: '{m/host}' };
    expect(limiterKey(decl('m', spec), 7)).not.toBe(limiterKey(decl('m', spec), 8));
    expect(limiterKey(decl('m', spec), null)).not.toBe(limiterKey(decl('m', spec), 7));
  });
});

const SMTP_SYSTEM = {
  'alerts/smtp_host': { type: 'string' },
  'alerts/smtp_port': { type: 'integer' },
  'alerts/smtp_user': { type: 'string' },
  'alerts/smtp_password': {
    type: 'obscure',
    tester: {
      kind: 'smtp',
      host: '{app_monitor/alerts/smtp_host}',
      port: '{app_monitor/alerts/smtp_port}',
      user: '{app_monitor/alerts/smtp_user}',
      pass: '{app_monitor/alerts/smtp_password}',
    },
  },
};

describe('normalizeTester', () => {
  it('turns a bare string into a named toolbox declaration', () => {
    expect(normalizeTester('graph_credentials')).toEqual({
      kind: 'toolbox', name: 'graph_credentials',
    });
  });

  it('passes an object through', () => {
    expect(normalizeTester({ kind: 'smtp', host: 'h' })).toEqual({ kind: 'smtp', host: 'h' });
  });

  it('is null for a missing, empty or non-declaration value', () => {
    expect(normalizeTester(undefined)).toBeNull();
    expect(normalizeTester('')).toBeNull();
    expect(normalizeTester(42)).toBeNull();
    expect(normalizeTester([])).toBeNull();
    expect(normalizeTester({})).toBeNull();          // no kind
  });
});

describe('placeholdersOf / interpolate', () => {
  const spec = {
    kind: 'http',
    url: '{jira/jira_host}/rest/api/2/myself',
    basic: ['{jira/jira_user}', '{jira/jira_token}'],
    headers: { Accept: 'application/json' },
    expect: 200,
  };

  it('finds every placeholder once, including nested ones', () => {
    expect(placeholdersOf(spec).sort()).toEqual(
      ['jira/jira_host', 'jira/jira_token', 'jira/jira_user'],
    );
  });

  it('substitutes into strings, arrays and nested objects', () => {
    const out = interpolate(spec, {
      'jira/jira_host': 'https://x.atlassian.net',
      'jira/jira_user': 'u@x',
      'jira/jira_token': 'tok',
    });
    expect(out.url).toBe('https://x.atlassian.net/rest/api/2/myself');
    expect(out.basic).toEqual(['u@x', 'tok']);
    expect(out.headers.Accept).toBe('application/json');
    expect(out.expect).toBe(200);
  });

  it('substitutes an unresolved placeholder with the empty string', () => {
    expect(interpolate({ url: '{m/h}/x' }, { 'm/h': null }).url).toBe('/x');
  });

  it('leaves a brace that is not a config path alone', () => {
    // A literal `{}` or `{not a path}` in a header value must survive.
    expect(interpolate({ url: 'https://x/{}' }, {}).url).toBe('https://x/{}');
  });
});

describe('findDeclaration', () => {
  const modules = [moduleDir('app_monitor', SMTP_SYSTEM)];

  it('finds the declaration on a nested field path', () => {
    const d = findDeclaration('app_monitor/alerts/smtp_password', modules);
    expect(d.module).toBe('app_monitor');
    expect(d.field).toBe('alerts/smtp_password');
    expect(d.kind).toBe('smtp');
  });

  it('is null for a field with no tester, an unknown field and an unknown module', () => {
    expect(findDeclaration('app_monitor/alerts/smtp_host', modules)).toBeNull();
    expect(findDeclaration('app_monitor/nope', modules)).toBeNull();
    expect(findDeclaration('nope/x', modules)).toBeNull();
  });

  it('is null for a path with no slash', () => {
    expect(findDeclaration('app_monitor', modules)).toBeNull();
  });

  it('survives a module whose system.json is not an object', () => {
    const bad = moduleDir('bad', []);
    expect(findDeclaration('bad/x', [bad])).toBeNull();
  });
});

describe('runConfigTest', () => {
  const modules = [moduleDir('app_monitor', SMTP_SYSTEM)];

  function deps(over = {}) {
    return {
      modules,
      loadOverrides: async () => ({
        'app_monitor/alerts/smtp_host': { value: 'mail', encrypted: false },
        'app_monitor/alerts/smtp_port': { value: '587', encrypted: false },
        'app_monitor/alerts/smtp_user': { value: 'u', encrypted: false },
        'app_monitor/alerts/smtp_password': { value: 'hunter2xyz', encrypted: false },
      }),
      probes: { smtp: { required: ['host'], probe: async () => ({ status: 'ok', code: 'OK' }) } },
      namedTests: new Map(),
      ...over,
    };
  }

  it('resolves the declared paths and runs the probe', async () => {
    let seen = null;
    const out = await runConfigTest({ path: 'app_monitor/alerts/smtp_password' }, deps({
      probes: {
        smtp: {
          required: ['host'],
          probe: async (spec) => { seen = spec; return { status: 'ok', code: 'OK' }; },
        },
      },
    }));
    expect(out.status).toBe('ok');
    expect(seen.host).toBe('mail');
    expect(seen.pass).toBe('hunter2xyz');
    expect(typeof out.ms).toBe('number');
  });

  it('redacts a resolved obscure value that the probe echoed back', async () => {
    const out = await runConfigTest({ path: 'app_monitor/alerts/smtp_password' }, deps({
      probes: {
        smtp: {
          required: ['host'],
          probe: async () => ({
            status: 'fail', code: 'AUTH_FAILED',
            detail: '535 5.7.8 rejected hunter2xyz',
          }),
        },
      },
    }));
    expect(out.status).toBe('fail');
    expect(out.detail).not.toContain('hunter2xyz');
    expect(out.detail).toContain('***');
  });

  it('is not_configured when a required field is empty', async () => {
    const out = await runConfigTest({ path: 'app_monitor/alerts/smtp_password' }, deps({
      loadOverrides: async () => ({}),
    }));
    expect(out.status).toBe('not_configured');
    expect(out.code).toBe('NOT_SET');
    expect(out.detail).toContain('alerts/smtp_host');   // says WHICH field
  });

  it('is error, not not_configured, when a value cannot be decrypted', async () => {
    // The whole point of the strict resolver: "stored but unreadable" must not
    // read as "nothing here".
    const out = await runConfigTest({ path: 'app_monitor/alerts/smtp_password' }, deps({
      loadOverrides: async () => ({
        'app_monitor/alerts/smtp_host': { value: 'mail', encrypted: false },
        'app_monitor/alerts/smtp_password': { value: 'not-a-ciphertext', encrypted: true },
      }),
    }));
    expect(out.status).toBe('error');
    expect(out.code).toBe('DECRYPT_FAILED');
    expect(out.detail).toContain('alerts/smtp_password');
  });

  it('errors when the field declares no tester', async () => {
    const out = await runConfigTest({ path: 'app_monitor/alerts/smtp_host' }, deps());
    expect(out.status).toBe('error');
    expect(out.code).toBe('NO_TESTER');
  });

  it('errors on an unknown kind', async () => {
    const mods = [moduleDir('m', { f: { type: 'obscure', tester: { kind: 'telnet' } } })];
    const out = await runConfigTest({ path: 'm/f' }, deps({ modules: mods }));
    expect(out.status).toBe('error');
    expect(out.code).toBe('UNKNOWN_KIND');
  });

  it('refuses a placeholder that names another module', async () => {
    // Otherwise one module's Test button reads another module's secret.
    const mods = [moduleDir('m', {
      f: { type: 'obscure', tester: { kind: 'http', url: '{jira/jira_host}' } },
    })];
    const out = await runConfigTest({ path: 'm/f' }, deps({ modules: mods }));
    expect(out.status).toBe('error');
    expect(out.code).toBe('FOREIGN_PATH');
    expect(out.detail).toContain('jira/jira_host');
  });

  it('shares one cooldown across every field that names the same credential', async () => {
    // The alias bypass: a per-path limit lets a caller rotate through the six
    // Outlook Graph fields and drive six times the login rate at one account.
    const shared = { kind: 'smtp', host: '{m/host}', user: '{m/user}', password: '{m/pass_a}' };
    const mods = [moduleDir('m', {
      host: { type: 'string' },
      user: { type: 'string' },
      pass_a: { type: 'obscure', tester: shared },
      pass_b: { type: 'obscure', tester: { ...shared } },
    })];
    const limiter = new ProbeLimiter({ cooldownMs: 3_000, clock: () => 1_000_000 });
    // Configured, so the first call reaches the probe: the cooldown is spent on
    // an ATTEMPT, never on a verdict this side reached by itself.
    const d = deps({
      modules: mods,
      limiter,
      loadOverrides: async () => ({
        'm/host': { value: 'mail', encrypted: false },
        'm/user': { value: 'u', encrypted: false },
        'm/pass_a': { value: 'p', encrypted: false },
        'm/pass_b': { value: 'p', encrypted: false },
      }),
    });

    expect((await runConfigTest({ path: 'm/pass_a' }, d)).code).not.toBe('COOLDOWN');
    const aliased = await runConfigTest({ path: 'm/pass_b' }, d);
    expect(aliased.status).toBe('error');
    expect(aliased.code).toBe('COOLDOWN');
  });

  it.each([
    ['a duplicate probe name', 'DUPLICATE_TESTER',
      { f: { type: 'obscure', tester: 'named' } },
      { namedTests: new Map([['named', { conflict: true }]]) }],
    ['unreachable stored config', 'CONFIG_UNAVAILABLE',
      { f: { type: 'obscure', tester: 'named' } },
      { loadOverrides: async () => { throw new Error('down'); } }],
    ['an empty required field', 'NOT_SET',
      { host: { type: 'string' }, f: { type: 'obscure', tester: { kind: 'smtp', host: '{m/host}' } } },
      { loadOverrides: async () => ({}) }],
  ])('spends no cooldown on %s', async (_label, code, system, over) => {
    // The cooldown protects the REMOTE ACCOUNT. Taking it before these checks
    // spent it on answers that authenticated against nothing, so the operator
    // saw the real diagnosis once and COOLDOWN for the next three seconds.
    const mods = [moduleDir('m', system)];
    const limiter = new ProbeLimiter({ cooldownMs: 3_000, clock: () => 1_000_000 });
    const d = deps({ modules: mods, limiter, ...over });

    expect((await runConfigTest({ path: 'm/f' }, d)).code).toBe(code);
    // Same code on the retry — not COOLDOWN masking it — and no entry taken.
    expect((await runConfigTest({ path: 'm/f' }, d)).code).toBe(code);
    expect(limiter.size).toBe(0);
  });

  it('a path that declares no tester adds no limiter entry', async () => {
    // Otherwise a caller invents paths and grows the limiter without limit.
    const mods = [moduleDir('m', { f: { type: 'obscure' } })];
    const limiter = new ProbeLimiter({ cooldownMs: 3_000, clock: () => 1_000_000 });
    const out = await runConfigTest({ path: 'm/f' }, deps({ modules: mods, limiter }));
    expect(out.code).toBe('NO_TESTER');
    expect(limiter.size).toBe(0);
  });

  it('refuses a local-kind declaration — that arm belongs to the framework', async () => {
    const mods = [moduleDir('m', {
      f: { type: 'obscure', tester: { kind: 'local', class: 'src.testers.x.T' } },
    })];
    const out = await runConfigTest({ path: 'm/f' }, deps({ modules: mods }));
    expect(out.status).toBe('error');
    expect(out.code).toBe('NOT_A_TOOLBOX_TEST');
  });

  it('runs a named module probe with its own resolved config', async () => {
    const mods = [moduleDir('outlook', {
      outlook_client_secret: { type: 'obscure', tester: 'graph_credentials' },
      outlook_tenant_id: { type: 'string' },
    })];
    let seen = null;
    const out = await runConfigTest({ path: 'outlook/outlook_client_secret' }, deps({
      modules: mods,
      loadOverrides: async () => ({
        'outlook/outlook_tenant_id': { value: 't', encrypted: false },
        'outlook/outlook_client_secret': { value: 'sec', encrypted: false },
      }),
      namedTests: new Map([['graph_credentials', {
        module: 'outlook',
        paths: ['outlook/outlook_tenant_id', 'outlook/outlook_client_secret'],
        run: async ({ config }) => { seen = config; return { status: 'ok', code: 'OK' }; },
      }]]),
    }));
    expect(out.status).toBe('ok');
    // Keyed by FIELD name, so an existing module helper (createGraphAuth) takes
    // it unchanged.
    expect(seen).toEqual({ outlook_tenant_id: 't', outlook_client_secret: 'sec' });
  });

  it('errors when a named probe is not registered', async () => {
    const mods = [moduleDir('outlook', {
      outlook_client_secret: { type: 'obscure', tester: 'graph_credentials' },
    })];
    const out = await runConfigTest({ path: 'outlook/outlook_client_secret' }, deps({
      modules: mods, namedTests: new Map(),
    }));
    expect(out.status).toBe('error');
    expect(out.code).toBe('UNKNOWN_TESTER');
  });

  it('refuses a named probe registered by a different module', async () => {
    const mods = [moduleDir('m', { f: { type: 'obscure', tester: 'someone_elses' } })];
    const out = await runConfigTest({ path: 'm/f' }, deps({
      modules: mods,
      namedTests: new Map([['someone_elses', {
        module: 'jira', paths: [], run: async () => ({ status: 'ok' }),
      }]]),
    }));
    expect(out.status).toBe('error');
    expect(out.code).toBe('FOREIGN_TESTER');
  });

  it('turns a probe that throws into an error, not a crash', async () => {
    const out = await runConfigTest({ path: 'app_monitor/alerts/smtp_password' }, deps({
      probes: { smtp: { required: ['host'], probe: async () => { throw new Error('boom'); } } },
    }));
    expect(out.status).toBe('error');
    expect(out.code).toBe('PROBE_CRASHED');
  });

  it('redacts a secret out of a crashing probe message too', async () => {
    const out = await runConfigTest({ path: 'app_monitor/alerts/smtp_password' }, deps({
      probes: {
        smtp: {
          required: ['host'],
          probe: async () => { throw new Error('failed with hunter2xyz'); },
        },
      },
    }));
    expect(out.detail).not.toContain('hunter2xyz');
  });

  it('stops a hanging probe at the timeout', async () => {
    const out = await runConfigTest({ path: 'app_monitor/alerts/smtp_password' }, deps({
      timeoutMs: 20,
      probes: {
        smtp: {
          required: ['host'],
          probe: () => new Promise((resolve) => setTimeout(() => resolve({ status: 'ok' }), 5000)),
        },
      },
    }));
    expect(out.status).toBe('error');
    expect(out.code).toBe('PROBE_TIMEOUT');
  });

  it('aborts the probe when the timeout fires, not just the wait for it', async () => {
    // Racing promises stops the WAIT; only the signal stops the login. A probe
    // that kept authenticating after PROBE_TIMEOUT drove the remote account
    // outside the per-credential cooldown that exists to bound it.
    let aborted = false;
    const out = await runConfigTest({ path: 'app_monitor/alerts/smtp_password' }, deps({
      timeoutMs: 20,
      probes: {
        smtp: {
          required: ['host'],
          probe: (spec, { signal }) => new Promise((resolve) => {
            signal.addEventListener('abort', () => { aborted = true; });
            setTimeout(() => resolve({ status: 'ok' }), 5000);
          }),
        },
      },
    }));
    expect(out.code).toBe('PROBE_TIMEOUT');
    expect(aborted).toBe(true);
  });

  it('hands the same abort signal to a named module probe', async () => {
    const mods = [moduleDir('m', { f: { type: 'obscure', tester: 'named' } })];
    let aborted = false;
    const out = await runConfigTest({ path: 'm/f' }, deps({
      modules: mods,
      timeoutMs: 20,
      loadOverrides: async () => ({ 'm/f': { value: 'v', encrypted: false } }),
      namedTests: new Map([['named', {
        module: 'm',
        paths: ['m/f'],
        run: ({ signal }) => new Promise((resolve) => {
          signal.addEventListener('abort', () => { aborted = true; });
          setTimeout(() => resolve({ status: 'ok' }), 5000);
        }),
      }]]),
    }));
    expect(out.code).toBe('PROBE_TIMEOUT');
    expect(aborted).toBe(true);
  });

  it('never lets a probe result carry a secret out through a field other than detail', async () => {
    // A named probe is handed obscure values. `code` is printed unredacted by
    // contract and copied into the REST log, `ms` is returned as-is, and the
    // BAD_RESULT message must not echo what it rejected.
    const mods = [moduleDir('m', { f: { type: 'obscure', tester: 'named' } })];
    const run = async (result) => runConfigTest({ path: 'm/f' }, deps({
      modules: mods,
      loadOverrides: async () => ({ 'm/f': { value: 'HUNTER2XYZ', encrypted: false } }),
      namedTests: new Map([['named', { module: 'm', paths: ['m/f'], run: async () => result }]]),
    }));

    // A secret shaped like a code passes CODE_SHAPE — it must still not print.
    const viaCode = await run({ status: 'fail', code: 'HUNTER2XYZ' });
    expect(viaCode.code).toBe('UNSPECIFIED');
    expect(JSON.stringify(viaCode)).not.toContain('HUNTER2XYZ');

    // A secret in the status: rejected, and the rejection does not quote it.
    const viaStatus = await run({ status: 'HUNTER2XYZ' });
    expect(viaStatus.code).toBe('BAD_RESULT');
    expect(JSON.stringify(viaStatus)).not.toContain('HUNTER2XYZ');

    // A secret in `ms`: replaced by the measured duration.
    const viaMs = await run({ status: 'ok', code: 'OK', ms: 'HUNTER2XYZ' });
    expect(typeof viaMs.ms).toBe('number');
    expect(JSON.stringify(viaMs)).not.toContain('HUNTER2XYZ');
  });

  it('reports a probe that answered with nothing as a bad result, not a timeout', async () => {
    const mods = [moduleDir('m', { f: { type: 'obscure', tester: 'named' } })];
    for (const result of [null, undefined, 'ok', 42, {}]) {
      const out = await runConfigTest({ path: 'm/f' }, deps({
        modules: mods,
        loadOverrides: async () => ({ 'm/f': { value: 'v', encrypted: false } }),
        namedTests: new Map([['named', { module: 'm', paths: ['m/f'], run: async () => result }]]),
      }));
      expect(out.code).toBe('BAD_RESULT');
    }
  });

  it('refuses a status outside the four-state vocabulary', async () => {
    // A named probe is third-party module code. A status the framework cannot
    // render must come back as "could not check", not be passed through.
    const mods = [moduleDir('m', { f: { type: 'obscure', tester: 'named' } })];
    for (const status of ['okay', 'OK', 'degraded', undefined, null, 1]) {
      const out = await runConfigTest({ path: 'm/f' }, deps({
        modules: mods,
        loadOverrides: async () => ({ 'm/f': { value: 'v', encrypted: false } }),
        namedTests: new Map([['named', {
          module: 'm', paths: ['m/f'], run: async () => ({ status, code: 'OK' }),
        }]]),
      }));
      expect(out.status).toBe('error');
      expect(out.code).toBe('BAD_RESULT');
    }
  });

  it('reports a config read that failed as could-not-check, never as a verdict', async () => {
    // The strict loader throws where the lenient one warns: falling back to
    // global config would answer about a credential the caller never named.
    const out = await runConfigTest({ path: 'app_monitor/alerts/smtp_password' }, deps({
      loadOverrides: async () => { const e = new Error('gone'); e.name = 'UnknownAgentView'; throw e; },
    }));
    expect(out.status).toBe('error');
    expect(out.code).toBe('CONFIG_UNAVAILABLE');
  });

  it('cannot be told it timed out by the probe it is running', async () => {
    // The timeout marker used to be a `{__timedOut: true}` field read off the
    // raced value BEFORE the result parser, so a probe could label its own
    // answer PROBE_TIMEOUT. The marker is a private Symbol now.
    const mods = [moduleDir('m', { f: { type: 'obscure', tester: 'named' } })];
    const out = await runConfigTest({ path: 'm/f' }, deps({
      modules: mods,
      loadOverrides: async () => ({ 'm/f': { value: 'v', encrypted: false } }),
      namedTests: new Map([['named', {
        module: 'm', paths: ['m/f'],
        run: async () => ({ status: 'ok', code: 'OK', __timedOut: true }),
      }]]),
    }));
    expect(out.status).toBe('ok');
    expect(out.code).toBe('OK');
  });

  it('reports a probe name registered twice instead of picking one', async () => {
    const mods = [moduleDir('m', { f: { type: 'obscure', tester: 'named' } })];
    const out = await runConfigTest({ path: 'm/f' }, deps({
      modules: mods,
      namedTests: new Map([['named', { conflict: true }]]),
    }));
    expect(out.status).toBe('error');
    expect(out.code).toBe('DUPLICATE_TESTER');
  });

  it('never returns a code that could carry prose', async () => {
    const out = await runConfigTest({ path: 'app_monitor/alerts/smtp_password' }, deps({
      probes: {
        smtp: {
          required: ['host'],
          probe: async () => ({ status: 'fail', code: 'not a code: 535 rejected!' }),
        },
      },
    }));
    expect(out.code).toMatch(/^[A-Z0-9_]{1,40}$/);
  });

  it('passes the agent_view id to the override loader', async () => {
    let seenId = 'unset';
    await runConfigTest({ path: 'app_monitor/alerts/smtp_password', agentViewId: 7 }, deps({
      loadOverrides: async (id) => {
        seenId = id;
        return { 'app_monitor/alerts/smtp_host': { value: 'mail', encrypted: false } };
      },
    }));
    expect(seenId).toBe(7);
  });
});

describe('registerConfigTests', () => {
  // Registration is a trust boundary: `configTests` is module metadata reaching
  // the framework, and `module:validate` reads manifests only — it cannot see a
  // JavaScript export.
  const withExport = (dir, name, body) => {
    fs.writeFileSync(path.join(dir, 'module.json'), JSON.stringify({ name, version: '0.1.0' }));
    fs.mkdirSync(path.join(dir, 'toolbox'), { recursive: true });
    fs.writeFileSync(path.join(dir, 'toolbox', 'probe.js'), body);
    return { name, _path: dir };
  };
  const tmp = (n) => fs.mkdtempSync(path.join(os.tmpdir(), `cfgreg-${n}-`));

  it('drops a declaration whose paths are not config paths', async () => {
    // `paths: [7]` reached `p.startsWith` inside path resolution and threw a
    // TypeError out of the route, which is not one of the four states.
    const mod = withExport(tmp('bad'), 'm',
      "export const configTests = [{ name: 'p', paths: [7], run: async () => ({status:'ok'}) }];");
    const registry = await registerConfigTests([mod]);
    expect(registry.has('p')).toBe(false);
  });

  it('drops a declaration with no run function or no name', async () => {
    const mod = withExport(tmp('shape'), 'm',
      "export const configTests = [{ name: 'p', paths: [] }, { paths: [], run: async () => ({}) }];");
    const registry = await registerConfigTests([mod]);
    expect(registry.size).toBe(0);
  });

  it('accepts a well-formed declaration and defaults absent paths', async () => {
    const mod = withExport(tmp('ok'), 'm',
      "export const configTests = [{ name: 'p', run: async () => ({status:'ok'}) }];");
    const registry = await registerConfigTests([mod]);
    expect(registry.get('p').paths).toEqual([]);
    expect(registry.get('p').module).toBe('m');
  });

  it('drops a non-array export and still registers a later valid module', async () => {
    // `for...of` over `configTests = {name, run}` threw a TypeError out of
    // registration, so ONE module written that way emptied the whole registry and
    // every other module's probe answered UNKNOWN_TESTER.
    const bad = withExport(tmp('nonarray'), 'bad',
      "export const configTests = { name: 'b', run: async () => ({status:'ok'}) };");
    const good = withExport(tmp('good'), 'good',
      "export const configTests = [{ name: 'g', run: async () => ({status:'ok'}) }];");
    const registry = await registerConfigTests([bad, good]);
    expect(registry.has('b')).toBe(false);
    expect(registry.get('g').module).toBe('good');
  });

  it('trims a padded name so it matches the system.json tester string', async () => {
    const mod = withExport(tmp('pad'), 'm',
      "export const configTests = [{ name: '  p  ', run: async () => ({status:'ok'}) },"
      + " { name: '   ', run: async () => ({status:'ok'}) }];");
    const registry = await registerConfigTests([mod]);
    expect([...registry.keys()]).toEqual(['p']);
  });

  it('fails closed when one name is registered twice', async () => {
    // First-wins made the probe that ran depend on readdir order.
    const dir = tmp('dupe');
    const mod = withExport(dir, 'm',
      "export const configTests = [{ name: 'p', run: async () => ({status:'ok'}) }];");
    fs.writeFileSync(path.join(dir, 'toolbox', 'other.js'),
      "export const configTests = [{ name: 'p', run: async () => ({status:'fail'}) }];");
    const registry = await registerConfigTests([mod]);
    expect(registry.get('p')).toEqual({ conflict: true });
  });
});
