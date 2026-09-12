import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, mkdir, writeFile, rm, symlink, readFile } from 'node:fs/promises';
import fsp from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { Readable } from 'node:stream';
import { tmpdir } from 'node:os';
import path from 'node:path';
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
  it('serves the current version at /<code>/', async () => {
    await start();
    const res = await get('/site/');
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toMatch(/text\/html/);
    expect(await res.text()).toBe(`<h1>${V2}</h1>`);
  });

  it('serves a non-current version at /<code>/v/<id>/', async () => {
    await start();
    const res = await get(`/site/v/${V1}/`);
    expect(res.status).toBe(200);
    expect(await res.text()).toBe(`<h1>${V1}</h1>`);
  });

  it('lists the artifact codes at /', async () => {
    await mkdir(path.join(root, 'other'), { recursive: true });
    await start();
    const body = await (await get('/')).text();
    expect(body).toContain('href="/site/"');
    expect(body).toContain('href="/other/"');
  });

  it('never lists the scratch directory', async () => {
    await mkdir(path.join(root, '.tmp'), { recursive: true });
    await start();
    expect(await (await get('/')).text()).not.toContain('.tmp');
  });

  it('answers 404 for an unknown code and for a pruned version', async () => {
    await start();
    expect((await get('/nope/')).status).toBe(404);
    expect((await get(`/site/v/v-20250101-000000-zzzz/`)).status).toBe(404);
  });

  it('answers 404 for a version id that is not a version id', async () => {
    await start();
    expect((await get('/site/v/..%2F..%2Fetc/')).status).toBe(404);
    expect((await get('/site/v/anything/')).status).toBe(404);
  });

  it('answers 405 for a write method', async () => {
    await start();
    expect((await fetch(`${base}/site/`, { method: 'POST' })).status).toBe(405);
  });
});

describe('directory urls', () => {
  // Without the redirect a relative `app.js` under /site/sub resolves to /site/app.js.
  it('redirects a directory url that has no trailing slash, keeping the query', async () => {
    await mkdir(path.join(root, 'site', 'v', V2, 'sub'));
    await writeFile(path.join(root, 'site', 'v', V2, 'sub', 'index.html'), '<h1>sub</h1>');
    await start();
    const res = await fetch(`${base}/site/sub?x=1`, { redirect: 'manual' });
    expect(res.status).toBe(301);
    expect(res.headers.get('location')).toBe('/site/sub/?x=1');
  });

  it('redirects the bare code and the bare version directory too', async () => {
    await start();
    expect((await fetch(`${base}/site`, { redirect: 'manual' })).headers.get('location')).toBe('/site/');
    expect((await fetch(`${base}/site/v/${V1}`, { redirect: 'manual' })).headers.get('location'))
      .toBe(`/site/v/${V1}/`);
  });
});

describe('containment', () => {
  // `send` does zero lstat/realpath and served a file through a symlink pointing
  // outside the root, which is why the containment check is ours and not the
  // static handler's.
  it('refuses a symlink that points outside the published root', async () => {
    await symlink('/etc/passwd', path.join(root, 'site', 'v', V2, 'escape.txt'));
    await start();
    const res = await get('/site/escape.txt');
    expect(res.status).toBe(403);
    expect(await res.text()).not.toContain('root:');
  });

  it('never returns the content of a dotfile', async () => {
    await writeFile(path.join(root, 'site', 'v', V2, '.env'), 'SECRET=1');
    await start();
    const res = await get('/site/.env');
    expect(res.status).toBe(404);
    expect(await res.text()).not.toContain('SECRET');
  });

  it('never escapes the root through a traversal segment', async () => {
    await start();
    expect((await get('/site/..%2F..%2Fetc%2Fpasswd')).status).toBe(404);
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
        if (thrown === 0 && String(p).includes('current')) {
          thrown += 1;
          const err = new Error('einval'); err.code = 'EINVAL'; throw err;
        }
        return fsp.realpath(p);
      },
    };
    await start({ fs });
    const res = await get('/site/');
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
    expect((await get('/site/')).status).toBe(404);
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
    await get('/site/').then((r) => r.text()).catch(() => {});
    expect((await get('/site/')).status).toBe(200);
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
    const res = await fetch(`${base}/site/`, { signal: ac.signal });
    const reader = res.body.getReader();
    expect((await reader.read()).value).toHaveLength(1);
    expect(streams).toHaveLength(1);
    expect(streams[0].destroyed).toBe(false);

    ac.abort();
    await until(() => streams[0].destroyed);
  });

  it('never lets a filename forge a second log line', async () => {
    // The name is attacker-chosen: an agent picks the filenames a version contains, and
    // the URL decodes back to one. Unbounded, the `\n` splits the record and the rest is
    // whatever the attacker wants an operator to read.
    const evil = 'a\nartifacts: pruned everything, nothing to see';
    await writeFile(path.join(root, 'site', 'v', V2, evil), 'x');
    const openRead = (p) => {
      const s = createReadStream(p);
      s.once('open', () => s.destroy(Object.assign(new Error('boom'), { code: 'EIO' })));
      return s;
    };
    await start({ openRead });

    const lines = [];
    const real = process.stderr.write;
    process.stderr.write = (chunk, enc, cb) => {
      lines.push(String(chunk));
      const done = typeof enc === 'function' ? enc : cb;
      if (done) done();
      return true;
    };
    try {
      await get(`/site/${encodeURIComponent(evil)}`).then((r) => r.text()).catch(() => {});
    } finally {
      process.stderr.write = real;
    }

    const mine = lines.filter((l) => l.startsWith('artifacts: read failed'));
    expect(mine).toHaveLength(1);
    expect(mine[0].endsWith('\n')).toBe(true);
    expect(mine[0].slice(0, -1)).not.toContain('\n');
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
    expect((await get('/site/')).status).toBe(503);
    await gate(JSON.stringify({ versioned_artifacts: true }));
    expect((await get('/site/')).status).toBe(200);
  });

  // `app/etc/modules.json` lists only explicitly toggled modules, so absence is
  // "enabled". This mirrors module enablement, NOT the `is_enabled` tool gate,
  // which is the one that fails closed.
  it('serves when the file is absent, the key is absent, or the file is unparseable', async () => {
    await start();
    expect((await get('/site/')).status).toBe(200);
    await gate(JSON.stringify({ jira: false }));
    expect((await get('/site/')).status).toBe(200);
    await gate('{"versioned_artifacts": fal');
    expect((await get('/site/')).status).toBe(200);
  });
});
