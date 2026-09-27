import http from 'node:http';
import path from 'node:path';
import fsp from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { pipeline } from 'node:stream/promises';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { ARTIFACT_CODE_RE } from '../toolbox/paths.js';
import { boundedLine } from '../toolbox/errors.js';
import { verifyCredential } from '../toolbox/auth.js';
import { parseServedPath } from './served-path.js';
import {
  AUTH_FAILURES_PER_ADDRESS, REQUESTS_PER_CREDENTIAL, WINDOW_MS, createWindowCounter,
} from './rate-limit.js';

// Under `server/`, NOT `toolbox/`: `src/agento/toolbox/config-loader.js` imports every
// `.js` in a module's `toolbox/` into the secrets container. This process holds no
// secret, no DB handle and no framework code — it reads a directory tree and answers
// HTTP. `paths.js`, `errors.js` and `auth.js` are the only imports it shares with the
// store side, and all are pure — none reaches Git, the audit sink or a DB handle.
//
// Plain `node:http`, no express: `express` is a toolbox dependency that does not resolve
// under vitest (`Cannot find module './lib/express'` — vite maps the bare specifier to a
// phantom path at the vitest root), so an express version of this file could not be
// tested at all. The containment check below is ours either way, which is what
// `express.static` would mostly have been buying.

// ONE write site, so "a log record is one bounded line" is a property of this file
// rather than of each call. Every part is attacker-chosen text: a version holds the
// filenames an agent put in it, and the URL decodes back to one. A raw `\n` in either
// forges a second record, which is how an operator reads a sentence nobody wrote.
const logLine = (...parts) => process.stderr.write(`artifacts: ${parts.map(boundedLine).join(' ')}\n`);

const MIME = {
  '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8', '.md': 'text/plain; charset=utf-8',
  '.csv': 'text/csv; charset=utf-8', '.xml': 'application/xml; charset=utf-8',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
  '.avif': 'image/avif', '.ico': 'image/x-icon', '.pdf': 'application/pdf',
  '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf',
  '.mp4': 'video/mp4', '.webm': 'video/webm', '.mp3': 'audio/mpeg',
};

/** ENOENT and ENOTDIR mean "not there". Every other errno means the answer is UNKNOWN;
 *  reporting unknown as absent is the same defect the store side guards against. */
const isMissing = (err) => err?.code === 'ENOENT' || err?.code === 'ENOTDIR';
const isTransient = (err) => err?.code === 'EINVAL' || err?.code === 'ESTALE';

const text = (res, code, body) => {
  res.writeHead(code, { 'content-type': 'text/plain; charset=utf-8' });
  res.end(body ?? `${code}\n`);
};

// A sidecar that is present but not a valid scrypt record: `verifyCredential` rejects
// every credential against it, so a corrupt `.auth` fails CLOSED (401) rather than
// serving the tree open. Distinct object so a fresh parse is never mistaken for it.
const INVALID_AUTH = Object.freeze({ algo: 'invalid' });

export function createArtifactsServer({
  root, etcDir, fs = fsp, openRead = createReadStream, verify = verifyCredential,
  limits = { authFailuresPerAddress: AUTH_FAILURES_PER_ADDRESS, requestsPerCredential: REQUESTS_PER_CREDENTIAL },
  now = Date.now,
} = {}) {
  let realRoot = null;
  // SEC-12. Failures are counted per client address. `proxy` is the only other container on
  // this network and it overwrites a client's X-Forwarded-For, so the header is its word;
  // without it (the healthcheck, a test) the socket address is the key.
  const failures = createWindowCounter({ limit: limits.authFailuresPerAddress, now });
  const perCredential = createWindowCounter({ limit: limits.requestsPerCredential, now });
  const sweeper = setInterval(() => { failures.sweep(); perCredential.sweep(); }, WINDOW_MS);
  sweeper.unref();
  const clientAddress = (req) => {
    const fwd = req.headers['x-forwarded-for'];
    const first = typeof fwd === 'string' ? fwd.split(',')[0].trim() : '';
    return first && first.length <= 64 ? first : (req.socket.remoteAddress ?? '');
  };
  // Re-read per request, stat-cached on mtime+size, so `mo:di` takes effect without a
  // restart: this container has no DB and no env_file and must keep it that way.
  let gate = { key: null, disabled: false };
  // Per-artifact Basic-auth sidecars, cached on mtime+size exactly like the gate — the
  // toolbox writes `published/<code>/.auth` and a `config:set` takes effect without a
  // restart. `null` cached means "checked, no auth".
  const authCache = new Map();

  async function disabled() {
    const file = path.join(etcDir, 'modules.json');
    let st;
    try { st = await fs.stat(file); } catch { return false; }
    const key = `${st.mtimeMs}:${st.size}`;
    if (key !== gate.key) {
      let off = false;
      // Absent file, absent key, unparseable file -> SERVE. `app/etc/modules.json` lists
      // only explicitly toggled modules, so absence is "enabled". This mirrors module
      // enablement, not the `is_enabled` tool gate, which is the one that fails closed.
      try { off = JSON.parse(await fs.readFile(file, 'utf8'))?.versioned_artifacts === false; } catch { off = false; }
      gate = { key, disabled: off };
    }
    return gate.disabled;
  }

  /** The auth sidecar for one artifact, or `null` when the tree is open. A present but
   *  unreadable/unparseable file returns `INVALID_AUTH`, which denies every credential —
   *  "auth is configured" must never degrade into "served open". */
  async function authFor(code) {
    const file = path.join(root, code, '.auth');
    let st;
    try { st = await fs.stat(file); }
    catch (err) { if (isMissing(err)) { authCache.delete(code); return null; } throw err; }
    const key = `${st.mtimeMs}:${st.size}`;
    const hit = authCache.get(code);
    if (hit && hit.key === key) return hit.sidecar;
    let sidecar;
    try { sidecar = JSON.parse(await fs.readFile(file, 'utf8')); }
    catch { sidecar = INVALID_AUTH; }
    authCache.set(code, { key, sidecar });
    return sidecar;
  }

  // `user:password` from a Basic header, or null. The password may itself contain a
  // colon, so only the FIRST one splits.
  function basicCredential(req) {
    const header = req.headers.authorization;
    if (typeof header !== 'string' || !/^basic /i.test(header)) return null;
    let decoded;
    try { decoded = Buffer.from(header.slice(6).trim(), 'base64').toString('utf8'); }
    catch { return null; }
    const i = decoded.indexOf(':');
    if (i < 0) return null;
    return { user: decoded.slice(0, i), password: decoded.slice(i + 1) };
  }

  function challenge(res, headOnly) {
    res.writeHead(401, {
      // A fixed realm — no artifact code, so no attacker-chosen text in a header.
      'www-authenticate': 'Basic realm="artifacts", charset="UTF-8"',
      'content-type': 'text/plain; charset=utf-8',
    });
    res.end(headOnly ? undefined : 'authentication required\n');
  }

  async function contained(p) {
    if (realRoot === null) realRoot = await fs.realpath(root);
    const real = await fs.realpath(p);
    return real === realRoot || real.startsWith(realRoot + path.sep) ? real : null;
  }

  async function serve(res, rel, ctx, retried = false) {
    let real; let st;
    try {
      real = await contained(path.join(root, rel));
      if (real === null) return text(res, 403, 'forbidden\n');
      st = await fs.stat(real);
      if (st.isDirectory()) {
        // Without this a relative `app.js` under /site/sub resolves to /site/app.js.
        if (ctx.redirectTo) { res.writeHead(301, { location: ctx.redirectTo }); return res.end(); }
        real = await contained(path.join(real, 'index.html'));
        if (real === null) return text(res, 403, 'forbidden\n');
        st = await fs.stat(real);
      }
      if (!st.isFile()) return text(res, 404);
    } catch (err) {
      // Measured: the first request after a symlink swap answered 500 through the macOS
      // VirtioFS mount and the identical retry answered 200.
      if (isTransient(err) && !retried) return serve(res, rel, ctx, true);
      if (isMissing(err) || isTransient(err) || err?.code === 'ELOOP') return text(res, 404);
      throw err;
    }
    res.writeHead(200, {
      'content-type': MIME[path.extname(real).toLowerCase()] ?? 'application/octet-stream',
      'content-length': st.size,
      // The tree is agent-authored and an extension this map does not know falls back to
      // `application/octet-stream` — which a sniffing browser will happily re-read as HTML
      // and run. The declared type is the whole type.
      'x-content-type-options': 'nosniff',
    });
    if (ctx.headOnly) return res.end();
    // `pipe` does not forward a read error, so an unhandled 'error' event took the whole
    // serving process down — and retention removing a version between the `stat` and the
    // open is enough to cause it. `pipeline` destroys both sides instead, in either
    // direction, which also releases the descriptor when the client goes away first.
    try {
      await pipeline(openRead(real), res);
    } catch (err) {
      res.destroy();
      if (err?.code !== 'ERR_STREAM_PREMATURE_CLOSE') {
        logLine('read failed for', rel, `- ${err?.code ?? err?.name ?? 'Error'}`);
      }
    }
  }

  /** The artifact a share token names, or null. `published/.shares/<token>` holds the code. */
  async function shareCode(token) {
    let code;
    try { code = (await fs.readFile(path.join(root, '.shares', token), 'utf8')).trim(); }
    catch (err) { if (isMissing(err)) return null; throw err; }
    return ARTIFACT_CODE_RE.test(code) ? code : null;
  }

  async function handle(req, res) {
    if (req.method !== 'GET' && req.method !== 'HEAD') return text(res, 405);
    if (await disabled()) return text(res, 503, 'versioned_artifacts is disabled\n');
    const headOnly = req.method === 'HEAD';
    const address = clientAddress(req);
    if (!failures.allowed(address)) return text(res, 429);
    const fail = (code) => { failures.take(address); return text(res, code); };

    // Routed on the RAW target: `new URL()` resolves `..` and `%2e%2e` before any check
    // could see them, which is the second parse §6.2 forbids.
    const route = parseServedPath(req.url);
    if (route === null) return fail(404);
    // `/` is the healthcheck. It lists nothing: no directory index anywhere (§6.3, §9).
    if (route.kind === 'health') return text(res, 200, 'ok\n');

    const search = req.url.includes('?') ? req.url.slice(req.url.indexOf('?')) : '';
    // Relative, so it is right on the apps origin (`/a/…`) and on a share origin alike.
    const ctx = { headOnly, redirectTo: route.trailing || route.last === null ? null : `${route.last}/${search}` };
    if (route.kind === 'app') {
      // Already authorized by web. Basic auth never applies here: Basic and launch
      // credentials must not meet on one origin (§9).
      return serve(res, [route.code, 'v', route.versionId, ...route.names].join('/'), ctx);
    }

    // A share: the token names the artifact, and it serves only while a Basic credential is set.
    const code = await shareCode(route.token);
    if (code === null) return fail(404);
    const sidecar = await authFor(code);
    if (!sidecar) return fail(404);
    const header = req.headers.authorization;
    if (typeof header === 'string'
        && !perCredential.take(createHash('sha256').update(header).digest('hex'))) return text(res, 429);
    const cred = basicCredential(req);
    if (!cred || !verify(sidecar, cred.user, cred.password)) {
      failures.take(address);
      return challenge(res, headOnly);
    }
    res.setHeader('referrer-policy', 'no-referrer');
    const tree = route.versionId ? ['v', route.versionId] : ['current'];
    return serve(res, [code, ...tree, ...route.names].join('/'), ctx);
  }

  const server = http.createServer((req, res) => {
    handle(req, res).catch((err) => {
      logLine(req.method, req.url, `- ${err?.code ?? err?.name ?? 'Error'}`);
      if (!res.headersSent) text(res, 500);
      else res.end();
    });
  });
  server.on('close', () => clearInterval(sweeper));
  return server;
}

// The service carries no `environment:` key, so these defaults are the only values
// reachable in the container; the env reads exist for the vitest harness.
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const server = createArtifactsServer({
    root: process.env.AGENTO_ARTIFACTS_ROOT ?? '/srv/published',
    etcDir: process.env.AGENTO_ARTIFACTS_ETC ?? '/app/etc',
  });
  server.listen(Number(process.env.AGENTO_ARTIFACTS_LISTEN ?? 8080), '0.0.0.0');
}
