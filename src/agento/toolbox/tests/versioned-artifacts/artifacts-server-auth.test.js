import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, mkdir, writeFile, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createArtifactsServer } from '../../../modules/versioned_artifacts/server/artifacts-server.js';
import { hashSecret } from '../../../modules/versioned_artifacts/toolbox/auth.js';

const V1 = 'v-20260101-120000-aaaa';

let root; let etcDir; let server; let base;

async function start() {
  server = createArtifactsServer({ root, etcDir });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${server.address().port}`;
}

const authHeader = (user, pass) => `Basic ${Buffer.from(`${user}:${pass}`).toString('base64')}`;
const get = (p, headers = {}) => fetch(`${base}${p}`, { headers });
const writeSidecar = (obj) => writeFile(path.join(root, 'site', '.auth'), JSON.stringify(obj));

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'va-auth-'));
  etcDir = await mkdtemp(path.join(tmpdir(), 'va-auth-etc-'));
  await mkdir(path.join(root, 'site', 'v', V1), { recursive: true });
  await writeFile(path.join(root, 'site', 'v', V1, 'index.html'), '<h1>secret</h1>');
  await symlink(path.join('v', V1), path.join(root, 'site', 'current'));
});

afterEach(async () => {
  if (server) { server.closeAllConnections(); await new Promise((r) => server.close(r)); }
  server = null;
  await rm(root, { recursive: true, force: true });
  await rm(etcDir, { recursive: true, force: true });
});

describe('basic auth', () => {
  it('serves openly when no sidecar is present', async () => {
    await start();
    expect((await get('/site/')).status).toBe(200);
  });

  it('challenges with 401 and WWW-Authenticate when a sidecar is present and no credential is sent', async () => {
    await writeSidecar({ user: 'site', ...hashSecret('pw') });
    await start();
    const res = await get('/site/');
    expect(res.status).toBe(401);
    expect(res.headers.get('www-authenticate')).toMatch(/^Basic realm=/);
  });

  it('accepts the right credential and rejects a wrong one', async () => {
    await writeSidecar({ user: 'site', ...hashSecret('pw') });
    await start();
    expect((await get('/site/', { authorization: authHeader('site', 'pw') })).status).toBe(200);
    expect((await get('/site/', { authorization: authHeader('site', 'nope') })).status).toBe(401);
    expect((await get('/site/', { authorization: authHeader('nobody', 'pw') })).status).toBe(401);
  });

  it('protects the versioned path too, not only current', async () => {
    await writeSidecar({ user: 'site', ...hashSecret('pw') });
    await start();
    expect((await get(`/site/v/${V1}/`)).status).toBe(401);
    expect((await get(`/site/v/${V1}/`, { authorization: authHeader('site', 'pw') })).status).toBe(200);
  });

  it('leaves the index / open even when an artifact is protected (healthcheck)', async () => {
    await writeSidecar({ user: 'site', ...hashSecret('pw') });
    await start();
    const res = await get('/');
    expect(res.status).toBe(200);
    expect(await res.text()).toContain('site');
  });

  it('never serves the .auth sidecar itself', async () => {
    await writeSidecar({ user: 'site', ...hashSecret('pw') });
    await start();
    expect((await get('/site/.auth', { authorization: authHeader('site', 'pw') })).status).toBe(404);
  });

  it('fails closed on a corrupt sidecar', async () => {
    await writeFile(path.join(root, 'site', '.auth'), 'not json');
    await start();
    expect((await get('/site/', { authorization: authHeader('site', 'pw') })).status).toBe(401);
  });

  it('picks up a credential change without a restart (mtime cache)', async () => {
    await writeSidecar({ user: 'site', ...hashSecret('old') });
    await start();
    expect((await get('/site/', { authorization: authHeader('site', 'old') })).status).toBe(200);
    // A later mtime, so the stat-cache key changes; base64url ids embed no clock here.
    await new Promise((r) => setTimeout(r, 10));
    await writeSidecar({ user: 'site', ...hashSecret('new') });
    expect((await get('/site/', { authorization: authHeader('site', 'old') })).status).toBe(401);
    expect((await get('/site/', { authorization: authHeader('site', 'new') })).status).toBe(200);
  });
});
