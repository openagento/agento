import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, mkdir, writeFile, rm, symlink, readFile } from 'node:fs/promises';
import fsp from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { Readable } from 'node:stream';
import { tmpdir } from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { createArtifactsServer } from '../../../modules/versioned_artifacts/server/artifacts-server.js';

const V1 = 'v-20260101-120000-aaaa';
const V2 = 'v-20260102-120000-bbbb';

let root; let etcDir; let server; let base;

async function start(opts = {}) {
  server = createArtifactsServer({ root, etcDir, ...opts });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${server.address().port}`;
}

const get = (p) => fetch(`${base}${p}`);
// fetch normalizes `..` itself; this sends the target byte for byte.
const rawGet = (p) => new Promise((resolve, reject) => {
  const req = http.request(`${base}/`, { path: p }, (res) => {
    let body = '';
    res.on('data', (c) => { body += c; });
    res.on('end', () => resolve({ status: res.statusCode, body }));
  });
  req.on('error', reject);
  req.end();
});
const gate = (body) => writeFile(path.join(etcDir, 'modules.json'), body);
// Polls instead of sleeping a fixed span: the teardown must not race a cleanup that
// has not happened yet, and a fixed sleep either flakes or costs every run.
async function until(cond, ms = 2000) {
  const stop = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > stop) throw new Error('condition not reached');
    await new Promise((r) => setTimeout(r, 10));
  }
}

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'va-pub-'));
  etcDir = await mkdtemp(path.join(tmpdir(), 'va-etc-'));
  for (const v of [V1, V2]) {
    await mkdir(path.join(root, 'site', 'v', v), { recursive: true });
    await writeFile(path.join(root, 'site', 'v', v, 'index.html'), `<h1>${v}</h1>`);
  }
  await symlink(path.join('v', V2), path.join(root, 'site', 'current'));
});

afterEach(async () => {
  // `fetch` keeps its socket alive, and `close()` waits for open connections — without
  // this the teardown sat on the keep-alive timeout and each test cost ~3 s.
  if (server) { server.closeAllConnections(); await new Promise((r) => server.close(r)); }
  server = null;
  await rm(root, { recursive: true, force: true });
  await rm(etcDir, { recursive: true, force: true });
});

describe('routing', () => {
  it('serves a version at /<code>/v/<id>/', async () => {
    await start();
    const res = await get(`/site/v/${V1}/`);
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toMatch(/text\/html/);
    expect(await res.text()).toBe(`<h1>${V1}</h1>`);
  });

  it('has no current route: /<code>/ is 404 even though current exists (§6.2)', async () => {
    await start();
    expect((await get('/site/')).status).toBe(404);
    expect((await get('/site/index.html')).status).toBe(404);
    expect((await get('/site/current/')).status).toBe(404);
  });

  it('answers / with ok and lists nothing (no directory index, §6.3)', async () => {
    await mkdir(path.join(root, 'other'), { recursive: true });
    await start();
    const res = await get('/');
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toBe('ok\n');
    expect(body).not.toContain('site');
  });

  it('declares the content type as final on every body it serves', async () => {
    // The tree is agent-authored: an unknown extension is served as
    // application/octet-stream, and a sniffing browser would re-read that as HTML.
    await start();
    expect((await get(`/site/v/${V1}/`)).headers.get('x-content-type-options')).toBe('nosniff');
  });

  it('answers 404 for an unknown code and for a pruned version', async () => {
    await start();
    expect((await get(`/nope/v/${V1}/`)).status).toBe(404);
    expect((await get(`/site/v/v-20250101-000000-zzzz/`)).status).toBe(404);
  });

  it('answers 404 for a version id that is not a version id', async () => {
    await start();
    expect((await get('/site/v/..%2F..%2Fetc/')).status).toBe(404);
    expect((await get('/site/v/anything/')).status).toBe(404);
  });

  it('answers 405 for a write method', async () => {
    await start();
    expect((await fetch(`${base}/site/v/${V1}/`, { method: 'POST' })).status).toBe(405);
  });

  it('never applies Basic auth on the app path, even with a sidecar (§9)', async () => {
    await writeFile(path.join(root, 'site', '.auth'), JSON.stringify({ user: 'x' }));
    await start();
    const res = await get(`/site/v/${V1}/`);
    expect(res.status).toBe(200);
    expect(res.headers.get('www-authenticate')).toBeNull();
  });
});

describe('directory urls', () => {
  // Without the redirect a relative `app.js` under /sub resolves one level up. The target
  // is relative, so it is right behind the apps origin's `/a` and a share's `/s/<token>`.
  it('redirects a directory url that has no trailing slash, keeping the query', async () => {
    await mkdir(path.join(root, 'site', 'v', V2, 'sub'));
    await writeFile(path.join(root, 'site', 'v', V2, 'sub', 'index.html'), '<h1>sub</h1>');
    await start();
    const res = await fetch(`${base}/site/v/${V2}/sub?x=1`, { redirect: 'manual' });
    expect(res.status).toBe(301);
    expect(res.headers.get('location')).toBe('sub/?x=1');
  });
});

describe('containment', () => {
  // `send` does zero lstat/realpath and served a file through a symlink pointing
  // outside the root, which is why the containment check is ours and not the
  // static handler's.
  it('refuses a symlink that points outside the published root', async () => {
    await symlink('/etc/passwd', path.join(root, 'site', 'v', V2, 'escape.txt'));
    await start();
    const res = await get(`/site/v/${V2}/escape.txt`);
    expect(res.status).toBe(403);
    expect(await res.text()).not.toContain('root:');
  });

  it('never returns the content of a dotfile', async () => {
    await writeFile(path.join(root, 'site', 'v', V2, '.env'), 'SECRET=1');
    await start();
    const res = await get(`/site/v/${V2}/.env`);
    expect(res.status).toBe(404);
    expect(await res.text()).not.toContain('SECRET');
  });

  it('never escapes the root through a traversal segment', async () => {
    await start();
    expect((await get(`/site/v/${V1}/..%2F..%2Fetc%2Fpasswd`)).status).toBe(404);
  });

  it('routes on the raw target: `..` and %2e%2e never reach another artifact (§6.2)', async () => {
    // WHATWG `new URL()` resolves both before a check could see them; the raw target does not.
    await mkdir(path.join(root, 'other', 'v', V1), { recursive: true });
    await writeFile(path.join(root, 'other', 'v', V1, 'index.html'), 'OTHER');
    await start();
    for (const p of [`/site/v/${V1}/../../../other/v/${V1}/`, `/site/v/${V1}/%2e%2e/%2e%2e/%2e%2e/other/v/${V1}/`]) {
      const res = await rawGet(p);
      expect(res.status).toBe(404);
      expect(res.body).not.toContain('OTHER');
    }
  });
});

describe('transient filesystem errors', () => {
  // The first request after a symlink swap returned 500 through the macOS
  // VirtioFS mount and the identical retry returned 200.
  it('retries once on EINVAL and then answers 200', async () => {
    let thrown = 0;
    const fs = {
      ...fsp,
      realpath: async (p) => {
        if (thrown === 0 && String(p).includes(V2)) {
          thrown += 1;
          const err = new Error('einval'); err.code = 'EINVAL'; throw err;
        }
        return fsp.realpath(p);
      },
    };
    await start({ fs });
    const res = await get(`/site/v/${V2}/`);
    expect(thrown).toBe(1);
    expect(res.status).toBe(200);
    expect(await res.text()).toBe(`<h1>${V2}</h1>`);
  });

  it('answers 404, never 500, when the retry fails too', async () => {
    const fs = {
      ...fsp,
      realpath: async () => { const e = new Error('estale'); e.code = 'ESTALE'; throw e; },
    };
    await start({ fs });
    expect((await get(`/site/v/${V2}/`)).status).toBe(404);
  });
});

describe('a read that fails after the response started', () => {
  // `pipe` does not forward a read error, so an unhandled 'error' event took the whole
  // serving process down. Retention removing a version between `stat` and `open` is
  // enough to cause it.
  it('does not take the process down, and the next request still succeeds', async () => {
    let fail = true;
    const openRead = (p) => {
      const s = createReadStream(p);
      // On 'open', so the failure is deterministic: the headers are already out and
      // no byte of this tiny file has been read yet.
      if (fail) {
        fail = false;
        s.once('open', () => s.destroy(Object.assign(new Error('boom'), { code: 'EIO' })));
      }
      return s;
    };
    await start({ openRead });
    await get(`/site/v/${V2}/`).then((r) => r.text()).catch(() => {});
    expect((await get(`/site/v/${V2}/`)).status).toBe(200);
  });

  it('destroys the source read when the client goes away mid-stream', async () => {
    const streams = [];
    // A source that hands over one byte and then stays open. A real file is too small to
    // still be streaming when the abort lands, so the old version of this test aborted
    // before the request even arrived and asserted `[].every(...)` — true, and proving
    // nothing. Here the read is provably OPEN when the client leaves.
    const openRead = () => {
      const s = new Readable({ read() {} });
      s.push('x');
      streams.push(s);
      return s;
    };
    await start({ openRead });
    const ac = new AbortController();
    const res = await fetch(`${base}/site/v/${V2}/`, { signal: ac.signal });
    const reader = res.body.getReader();
    expect((await reader.read()).value).toHaveLength(1);
    expect(streams).toHaveLength(1);
    expect(streams[0].destroyed).toBe(false);

    ac.abort();
    await until(() => streams[0].destroyed);
  });

  it('never lets a filename forge a second log line', async () => {
    // The name is attacker-chosen: an agent picks the filenames a version contains. A name
    // with a control character is refused before any read, so it cannot reach a log line.
    const evil = 'a\nartifacts: pruned everything, nothing to see';
    await writeFile(path.join(root, 'site', 'v', V2, evil), 'x');
    await start();
    const lines = [];
    const real = process.stderr.write;
    process.stderr.write = (chunk, enc, cb) => {
      lines.push(String(chunk));
      const done = typeof enc === 'function' ? enc : cb;
      if (done) done();
      return true;
    };
    let status;
    try {
      status = (await get(`/site/v/${V2}/${encodeURIComponent(evil)}`)).status;
    } finally {
      process.stderr.write = real;
    }
    expect(status).toBe(404);
    expect(lines.filter((l) => l.startsWith('artifacts:'))).toEqual([]);
  });
});

describe('module disablement gate', () => {
  it('answers 503 everywhere when the module is disabled, and keeps the content', async () => {
    await gate(JSON.stringify({ versioned_artifacts: false }));
    await start();
    expect((await get('/')).status).toBe(503);
    expect((await get('/site/')).status).toBe(503);
    expect((await get(`/site/v/${V1}/`)).status).toBe(503);
    // Disablement must not delete anything.
    expect(await readFile(path.join(root, 'site', 'v', V2, 'index.html'), 'utf8')).toBe(`<h1>${V2}</h1>`);
  });

  it('serves again when the value flips back, with no restart', async () => {
    await gate(JSON.stringify({ versioned_artifacts: false }));
    await start();
    expect((await get(`/site/v/${V1}/`)).status).toBe(503);
    await gate(JSON.stringify({ versioned_artifacts: true }));
    expect((await get(`/site/v/${V1}/`)).status).toBe(200);
  });

  // `app/etc/modules.json` lists only explicitly toggled modules, so absence is
  // "enabled". This mirrors module enablement, NOT the `is_enabled` tool gate,
  // which is the one that fails closed.
  it('serves when the file is absent, the key is absent, or the file is unparseable', async () => {
    await start();
    expect((await get(`/site/v/${V1}/`)).status).toBe(200);
    await gate(JSON.stringify({ jira: false }));
    expect((await get(`/site/v/${V1}/`)).status).toBe(200);
    await gate('{"versioned_artifacts": fal');
    expect((await get(`/site/v/${V1}/`)).status).toBe(200);
  });
});
