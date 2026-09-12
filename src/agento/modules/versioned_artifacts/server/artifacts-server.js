import http from 'node:http';
import path from 'node:path';
import fsp from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { pipeline } from 'node:stream/promises';
import { fileURLToPath } from 'node:url';
import { ARTIFACT_CODE_RE, VERSION_ID_RE } from '../toolbox/paths.js';
import { boundedLine } from '../toolbox/errors.js';

// Under `server/`, NOT `toolbox/`: `src/agento/toolbox/config-loader.js` imports every
// `.js` in a module's `toolbox/` into the secrets container. This process holds no
// secret, no DB handle and no framework code — it reads a directory tree and answers
// HTTP. `paths.js` and `errors.js` are the only imports it shares with the store side,
// and both are pure — neither reaches Git, the audit sink or a DB handle.
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

export function createArtifactsServer({ root, etcDir, fs = fsp, openRead = createReadStream } = {}) {
  let realRoot = null;
  // Re-read per request, stat-cached on mtime+size, so `mo:di` takes effect without a
  // restart: this container has no DB and no env_file and must keep it that way.
  let gate = { key: null, disabled: false };

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

  async function contained(p) {
    if (realRoot === null) realRoot = await fs.realpath(root);
    const real = await fs.realpath(p);
    return real === realRoot || real.startsWith(realRoot + path.sep) ? real : null;
  }

  async function index(res, headOnly) {
    let names;
    try { names = await fs.readdir(root); }
    catch (err) { if (isMissing(err)) names = []; else throw err; }
    // The filter is also the escaping: ARTIFACT_CODE_RE admits only [a-z0-9-].
    const codes = names.filter((n) => ARTIFACT_CODE_RE.test(n)).sort();
    const body = `<!doctype html><meta charset="utf-8"><title>Artifacts</title><ul>${
      codes.map((c) => `<li><a href="/${c}/">${c}</a></li>`).join('')}</ul>\n`;
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'content-length': Buffer.byteLength(body) });
    res.end(headOnly ? undefined : body);
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

  async function handle(req, res) {
    if (req.method !== 'GET' && req.method !== 'HEAD') return text(res, 405);
    if (await disabled()) return text(res, 503, 'versioned_artifacts is disabled\n');
    const headOnly = req.method === 'HEAD';

    let url; let pathname;
    try {
      url = new URL(req.url, 'http://artifacts');
      pathname = decodeURIComponent(url.pathname);
    } catch { return text(res, 400); }
    if (pathname.includes('\0')) return text(res, 400);

    const segs = pathname.split('/').filter(Boolean);
    // Kills dotfiles and `..` in one predicate, before any of it reaches the filesystem.
    if (segs.some((s) => s.startsWith('.'))) return text(res, 404);
    if (segs.length === 0) return index(res, headOnly);
    if (!ARTIFACT_CODE_RE.test(segs[0])) return text(res, 404);

    // Built from the RAW url so the redirect target stays encoded exactly as it came in.
    const ctx = {
      headOnly,
      redirectTo: url.pathname.endsWith('/') ? null : `${url.pathname}/${url.search}`,
    };
    if (segs[1] === 'v') {
      if (!VERSION_ID_RE.test(segs[2] ?? '')) return text(res, 404);
      return serve(res, segs.join('/'), ctx);
    }
    // A pruned version falls through to the ordinary 404 — no branch for it.
    return serve(res, [segs[0], 'current', ...segs.slice(1)].join('/'), ctx);
  }

  return http.createServer((req, res) => {
    handle(req, res).catch((err) => {
      logLine(req.method, req.url, `- ${err?.code ?? err?.name ?? 'Error'}`);
      if (!res.headersSent) text(res, 500);
      else res.end();
    });
  });
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
