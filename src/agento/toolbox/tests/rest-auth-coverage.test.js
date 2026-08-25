import { describe, it, expect, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// src/agento/toolbox/package.json declares "type": "module", so __dirname does not exist.
const here = path.dirname(fileURLToPath(import.meta.url));

// Every file under a module's toolbox/ — the routes live in api.js, but the handlers they
// mount (and the scope reads inside them) live in api-handlers.js / jira-proxy.js. Scanning
// api.js alone made these guards pass vacuously.
function moduleToolboxSources() {
  const modulesDir = path.join(here, '..', '..', 'modules');
  const files = [];
  for (const mod of fs.readdirSync(modulesDir)) {
    const dir = path.join(modulesDir, mod, 'toolbox');
    if (!fs.existsSync(dir)) continue;
    for (const name of fs.readdirSync(dir)) {
      if (!name.endsWith('.js')) continue;
      files.push({ mod, name, src: fs.readFileSync(path.join(dir, name), 'utf8') });
    }
  }
  return files;
}

// Structural guard for the class "a module REST route reachable without a capability".
// It asserts the SHAPE (a blanket /api guard installed before module registration),
// not the presence of any word, so a new module needs no change here.
describe('REST auth coverage', () => {
  it('installs the /api capability guard before module REST APIs are registered', () => {
    const src = fs.readFileSync(path.join(here, '..', 'server.js'), 'utf8');
    const guardAt = src.indexOf("app.use('/api'");
    const registerAt = src.indexOf('registerModuleRestApis(');
    expect(guardAt).toBeGreaterThan(-1);
    expect(registerAt).toBeGreaterThan(guardAt);
  });

  // The class: a guard added to a route that an existing client reaches with a credential
  // the route cannot read. Every SSE POST endpoint the SDK advertises must carry the
  // capability, because an MCP client posts to that string verbatim and sets no headers.
  it('advertises the capability on every SSE message endpoint it constructs', () => {
    const src = fs.readFileSync(path.join(here, '..', 'server.js'), 'utf8');
    const endpoints = [
      ...src.matchAll(/new SSEServerTransport\(\s*[`'"]([^`'"]*)/g),
    ].map(m => m[1]);
    expect(endpoints.length).toBeGreaterThan(0);
    expect(endpoints.filter(e => !e.includes('cap='))).toEqual([]);
  });

  it('no module registers a route outside the /api namespace', () => {
    const offenders = [];
    for (const { mod, name, src } of moduleToolboxSources()) {
      for (const m of src.matchAll(/app\.(get|post|put|patch|delete|all)\(\s*['"]([^'"]+)['"]/g)) {
        if (!m[2].startsWith('/api/')) offenders.push(`${mod}/${name}: ${m[1].toUpperCase()} ${m[2]}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  // The class the per-handler calls used to cover: a module route that answers a view the caller
  // never asked about. The check now lives in createModuleRouteApp, so it applies to every module
  // route — including a route a new module adds tomorrow with no code of its own.
  it('rejects a conflicting caller-supplied scope on every module route', async () => {
    const { createModuleRouteApp } = await import('../config-loader.js');
    const log = vi.fn();
    const routes = {};
    const app = { post: (route, ...hs) => { routes[route] = hs[hs.length - 1]; } };
    // Two handlers, as a real module registers them: a body parser then the handler itself.
    const handler = vi.fn((_req, res) => res.json({ ok: true }));
    createModuleRouteApp(app, log).post('/api/x', (req, _res, next) => next(), handler);

    const res = () => {
      const r = { code: 200, body: null };
      r.status = (c) => { r.code = c; return r; };
      r.json = (b) => { r.body = b; return r; };
      return r;
    };
    for (const req of [
      { capability: { agentViewId: 3 }, body: { agent_view_id: 9 } },
      { capability: { agentViewId: 3 }, query: { agent_view_id: '9' } },
      // A body that AGREES must not hide a query that disagrees.
      { capability: { agentViewId: 3 }, body: { agent_view_id: 3 }, query: { agent_view_id: '9' } },
    ]) {
      const r = res();
      await routes['/api/x'](req, r, vi.fn());
      expect(r.code).toBe(400);
    }
    expect(handler).not.toHaveBeenCalled();

    // An agreeing claim, and no claim at all, both reach the handler.
    for (const req of [
      { capability: { agentViewId: 3 }, body: { agent_view_id: 3 } },
      { capability: { agentViewId: 3 } },
    ]) {
      const r = res();
      await routes['/api/x'](req, r, vi.fn());
      expect(r.code).toBe(200);
    }
    expect(handler).toHaveBeenCalledTimes(2);
  });

  // The check must sit AFTER the body parsers, or there is no body to compare it with.
  it('guards the final handler only, never a body parser ahead of it', async () => {
    const { createModuleRouteApp } = await import('../config-loader.js');
    const seen = [];
    const app = { post: (_route, ...hs) => seen.push(...hs) };
    const parser = (req, _res, next) => next();
    const handler = (_req, res) => res.json({ ok: true });
    createModuleRouteApp(app, vi.fn()).post('/api/x', parser, handler);
    const r = { code: 200, status: (c) => { r.code = c; return r; }, json: () => r };
    // The FIRST handler is the parser: a conflicting claim must pass straight through it.
    seen[0]({ capability: { agentViewId: 3 }, body: { agent_view_id: 9 } }, r, vi.fn());
    expect(r.code).toBe(200);
  });

  it('no module REST handler derives its scope from the request body', () => {
    const offenders = [];
    for (const { mod, name, src } of moduleToolboxSources()) {
      for (const line of src.split('\n')) {
        if (/(body|query|req)\s*\.\s*(body\s*\.\s*)?agent_view_id/.test(line) && !/capability/.test(line)) {
          offenders.push(`${mod}/${name}: ${line.trim()}`);
        }
      }
    }
    // A body agent_view_id may only be COMPARED with req.capability, never used as the scope.
    expect(offenders).toEqual([]);
  });
});

// Every module route is registered through createModuleRouteApp. Express 4 does not forward an
// async handler's rejection to error middleware, so without this wrapper a strict-resolver throw
// would hang the request instead of answering. The module suites only assert that the error
// ESCAPES their handler — this is the one place that proves what happens to it afterwards.
describe('createModuleRouteApp — the /api error responder', () => {
  const fakeRes = () => {
    const res = { headersSent: false, code: null, body: null };
    res.status = (c) => { res.code = c; return res; };
    res.json = (b) => { res.body = b; res.headersSent = true; return res; };
    return res;
  };

  // Registers one route through the wrapper and returns the wrapped handler.
  // `log` collects every line the wrapper writes, so a test can assert what was NOT logged.
  const log = vi.fn();
  const wrapOne = async (handler, method = 'post') => {
    const { createModuleRouteApp } = await import('../config-loader.js');
    let wrapped;
    const app = { [method]: (_route, ...hs) => { wrapped = hs[hs.length - 1]; } };
    createModuleRouteApp(app, log)[method]('/api/x', handler);
    return wrapped;
  };

  it('answers 403 for a scope that no longer resolves', async () => {
    const { ScopeResolutionError } = await import('../config-loader.js');
    const res = fakeRes();
    await wrapOne(async () => { throw new ScopeResolutionError('gone'); }).then((h) => h({}, res, vi.fn()));
    expect(res.code).toBe(403);
    expect(res.body).toEqual({ error: 'capability scope unresolvable' });
  });

  it('answers 503 when the scope lookup itself failed', async () => {
    const { ScopeUnavailableError } = await import('../config-loader.js');
    const res = fakeRes();
    await wrapOne(async () => { throw new ScopeUnavailableError('db down'); }).then((h) => h({}, res, vi.fn()));
    expect(res.code).toBe(503);
  });

  // A generic failure must NOT borrow 403/503 — those two carry a specific meaning to the caller.
  it('answers 500 for any other rejection, and leaks no message', async () => {
    const res = fakeRes();
    await wrapOne(async () => { throw new Error('boom: postgres://u:p@h/db'); }).then((h) => h({}, res, vi.fn()));
    expect(res.code).toBe(500);
    expect(JSON.stringify(res.body)).not.toContain('postgres');
  });

  // The response is only half the exit. `err.message` is free text a driver wrote: mysql2 puts
  // the connection target in it, a Graph failure quotes the credential it used. "Log it
  // server-side" is not a safe destination for that, so the log gets a stable category.
  it('logs a stable category, never the raw error text', async () => {
    log.mockClear();
    const res = fakeRes();
    const err = new Error('connect ECONNREFUSED mysql://cron_agent:s3cret@10.0.0.4:3306/cron_agent');
    err.code = 'ECONNREFUSED';
    await wrapOne(async () => { throw err; }).then((h) => h({}, res, vi.fn()));
    const logged = log.mock.calls.map((c) => c.join(' ')).join('\n');
    for (const leak of ['mysql://', 's3cret', '10.0.0.4', 'cron_agent', 'ECONNREFUSED mysql']) {
      expect(logged).not.toContain(leak);
    }
    expect(logged).toContain('ECONNREFUSED');
  });

  // Same rule one layer up: the message the strict resolver puts on ScopeUnavailableError is
  // logged AND handed to the 503 responder, so it must not be built from the driver's text.
  it('builds ScopeUnavailableError from a category, not the driver message', async () => {
    const { loadScopedDbOverridesStrict } = await import('../config-loader.js');
    const db = await import('../db.js');
    const err = new Error('Access denied for user \'cron_agent\'@\'10.0.0.4\' (using password: YES)');
    err.code = 'ER_ACCESS_DENIED_ERROR';
    const spy = vi.spyOn(db, 'getCronPool').mockImplementation(() => { throw err; });
    await expect(loadScopedDbOverridesStrict(7)).rejects.toThrow(/ER_ACCESS_DENIED_ERROR/);
    await expect(loadScopedDbOverridesStrict(7)).rejects.not.toThrow(/cron_agent|password/);
    spy.mockRestore();
  });

  it('maps a SYNCHRONOUS throw the same way', async () => {
    const { ScopeResolutionError } = await import('../config-loader.js');
    const res = fakeRes();
    const h = await wrapOne(() => { throw new ScopeResolutionError('gone'); });
    h({}, res, vi.fn());
    expect(res.code).toBe(403);
  });

  // A handler that already answered and then failed must not have a second status written onto it.
  it('writes nothing once the response has been sent', async () => {
    const res = fakeRes();
    res.headersSent = true;
    await wrapOne(async () => { throw new Error('late'); }).then((h) => h({}, res, vi.fn()));
    expect(res.code).toBe(null);
  });

  // Express identifies error middleware by arity 4; wrapping one would change its arity and
  // silently unregister it as an error handler.
  it('passes an arity-4 error middleware through untouched', async () => {
    const errMw = (_err, _req, _res, _next) => {};
    expect(await wrapOne(errMw)).toBe(errMw);
  });
});

// The same class one layer out: a TRANSPORT route reachable without a capability. `/sse` opens
// the stream, but `/messages` is the half that carries the tool calls — it takes its sessionId
// from a QUERY STRING, a value that lands in access logs, so the id alone must never authorize.
describe('MCP transport auth coverage', () => {
  const serverSrc = fs.readFileSync(path.join(here, '..', 'server.js'), 'utf8');
  // Only bare health may answer without a capability: it reads no scope and returns no data.
  const UNGUARDED = new Set(['/health']);

  it('every route in server.js is mounted behind requireCapability', () => {
    const offenders = [];
    for (const m of serverSrc.matchAll(
      /app\.(get|post|put|patch|delete|all|use)\(\s*'([^']+)'\s*,\s*([^\n]*)/g,
    )) {
      const [, verb, route, rest] = m;
      if (UNGUARDED.has(route)) continue;
      if (!rest.includes('requireCapability')) offenders.push(`${verb.toUpperCase()} ${route}`);
    }
    expect(offenders).toEqual([]);
  });

  it('every live session lookup is followed by a capability-ownership check', () => {
    // A valid capability for view A must not drive a session opened by view B, so a session
    // entry carries the hash of the capability that created it and every reuse compares it.
    const lookups = [...serverSrc.matchAll(/(\w*[Ss]essions)\.get\(/g)].map((m) => m[1]);
    expect(lookups.length).toBeGreaterThan(0);
    for (const map of lookups) {
      const at = serverSrc.indexOf(`${map}.get(`);
      const window = serverSrc.slice(at, at + 600);
      expect(window, `${map}.get() without an ownership check`).toMatch(
        /capabilityHash !== tokenHash\(extractToken\(req\)\)/,
      );
    }
    // …and every session the maps hold was stored WITH that hash.
    for (const m of serverSrc.matchAll(/(\w*[Ss]essions)\.set\(([\s\S]{0,300}?)\);/g)) {
      expect(m[2], `${m[1]}.set() without a capabilityHash`).toContain('capabilityHash');
    }
  });
});
