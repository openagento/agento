import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, mkdir, writeFile, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createArtifactsServer } from '../../../modules/versioned_artifacts/server/artifacts-server.js';
import * as auth from '../../../modules/versioned_artifacts/toolbox/auth.js';

// Shares (PRD E6 §9, §9.1; DECISIONS 2026-09-27): `/s/<token>/…`, the token names the
// artifact through `published/.shares/<token>`, and a share serves only while a Basic
// credential is set.
const V1 = 'v-20260101-120000-aaaa';
const TOKEN = '0123456789abcdef0123456789abcdef';
const OTHER = 'fedcba9876543210fedcba9876543210';

let root; let etcDir; let server; let base;

async function start(opts = {}) {
  server = createArtifactsServer({ root, etcDir, ...opts });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${server.address().port}`;
}

const authHeader = (user, pass) => `Basic ${Buffer.from(`${user}:${pass}`).toString('base64')}`;
const get = (p, headers = {}) => fetch(`${base}${p}`, { headers });
const writeSidecar = (obj) => writeFile(path.join(root, 'site', '.auth'), JSON.stringify(obj));
const GOOD = () => ({ authorization: authHeader('site', 'pw') });

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'va-auth-'));
  etcDir = await mkdtemp(path.join(tmpdir(), 'va-auth-etc-'));
  await mkdir(path.join(root, 'site', 'v', V1), { recursive: true });
  await writeFile(path.join(root, 'site', 'v', V1, 'index.html'), '<h1>secret</h1>');
  await symlink(path.join('v', V1), path.join(root, 'site', 'current'));
  await mkdir(path.join(root, '.shares'));
  await writeFile(path.join(root, '.shares', TOKEN), 'site\n');
  await writeSidecar({ user: 'site', ...auth.hashSecret('pw'), share: TOKEN });
});

afterEach(async () => {
  if (server) { server.closeAllConnections(); await new Promise((r) => server.close(r)); }
  server = null;
  await rm(root, { recursive: true, force: true });
  await rm(etcDir, { recursive: true, force: true });
});

describe('share route', () => {
  it('challenges with 401 and WWW-Authenticate when no credential is sent', async () => {
    await start();
    const res = await get(`/s/${TOKEN}/`);
    expect(res.status).toBe(401);
    expect(res.headers.get('www-authenticate')).toMatch(/^Basic realm=/);
  });

  it('serves current and a version with the right credential, and sends no referrer or CORS', async () => {
    await start();
    for (const p of [`/s/${TOKEN}/`, `/s/${TOKEN}/v/${V1}/`]) {
      const res = await get(p, GOOD());
      expect(res.status).toBe(200);
      expect(await res.text()).toBe('<h1>secret</h1>');
      expect(res.headers.get('referrer-policy')).toBe('no-referrer');
      expect([...res.headers.keys()].filter((k) => k.startsWith('access-control-'))).toEqual([]);
    }
  });

  it('rejects a wrong credential', async () => {
    await start();
    expect((await get(`/s/${TOKEN}/`, { authorization: authHeader('site', 'nope') })).status).toBe(401);
    expect((await get(`/s/${TOKEN}/`, { authorization: authHeader('nobody', 'pw') })).status).toBe(401);
  });

  it('is 404 for an unknown token, a bad token shape, and a record naming a bad code', async () => {
    await writeFile(path.join(root, '.shares', OTHER), '../etc');
    await start();
    expect((await get(`/s/${'a'.repeat(32)}/`, GOOD())).status).toBe(404);
    expect((await get(`/s/${TOKEN.toUpperCase()}/`, GOOD())).status).toBe(404);
    expect((await get(`/s/${OTHER}/`, GOOD())).status).toBe(404);
  });

  it('serves nothing once the Basic credential is removed: a share needs one', async () => {
    await rm(path.join(root, 'site', '.auth'));
    await start();
    expect((await get(`/s/${TOKEN}/`, GOOD())).status).toBe(404);
  });

  it('never serves a dotfile, the sidecar included, and never walks out with ..', async () => {
    await start();
    expect((await get(`/s/${TOKEN}/.auth`, GOOD())).status).toBe(404);
    expect((await get(`/s/${TOKEN}/%2e%2e/%2e%2e/.shares/${TOKEN}`, GOOD())).status).toBe(404);
  });

  it('fails closed on a corrupt sidecar', async () => {
    await writeFile(path.join(root, 'site', '.auth'), 'not json');
    await start();
    expect((await get(`/s/${TOKEN}/`, GOOD())).status).toBe(404);
  });

  it('is 404 when the sidecar names another token: a stale record opens nothing', async () => {
    await writeSidecar({ user: 'site', ...auth.hashSecret('pw'), share: OTHER });
    await start();
    expect((await get(`/s/${TOKEN}/`, GOOD())).status).toBe(404);
  });

  it('picks up a credential change without a restart (mtime cache)', async () => {
    await writeSidecar({ user: 'site', ...auth.hashSecret('old'), share: TOKEN });
    await start();
    expect((await get(`/s/${TOKEN}/`, { authorization: authHeader('site', 'old') })).status).toBe(200);
    await new Promise((r) => setTimeout(r, 10));
    await writeSidecar({ user: 'site', ...auth.hashSecret('new'), share: TOKEN });
    expect((await get(`/s/${TOKEN}/`, { authorization: authHeader('site', 'old') })).status).toBe(401);
    expect((await get(`/s/${TOKEN}/`, { authorization: authHeader('site', 'new') })).status).toBe(200);
  });
});

describe('rate limits (SEC-12)', () => {
  let verified = 0;
  const verify = (...a) => { verified += 1; return auth.verifyCredential(...a); };
  const small = { verify, limits: { authFailuresPerAddress: 3, requestsPerCredential: 100 } };
  const from = (addr, headers = {}) => ({ 'x-forwarded-for': addr, ...headers });

  it('stops a flood of bad credentials by address, before any scrypt', async () => {
    await start(small);
    for (let i = 0; i < 3; i++) {
      expect((await get(`/s/${TOKEN}/`, from('10.0.0.1', { authorization: authHeader('site', `x${i}`) }))).status).toBe(401);
    }
    verified = 0;
    expect((await get(`/s/${TOKEN}/`, from('10.0.0.1', GOOD()))).status).toBe(429);
    expect(verified).toBe(0);
    // Another address is not throttled by the first one's failures.
    expect((await get(`/s/${TOKEN}/`, from('10.0.0.2', GOOD()))).status).toBe(200);
  });

  it('counts random unknown tokens with varied Authorization by address', async () => {
    await start(small);
    for (let i = 0; i < 3; i++) {
      const token = String(i).padStart(32, 'a');
      expect((await get(`/s/${token}/`, from('10.0.0.3', { authorization: `Basic ${i}` }))).status).toBe(404);
    }
    expect((await get(`/s/${TOKEN}/`, from('10.0.0.3', GOOD()))).status).toBe(429);
  });

  it('counts paths the validator refuses', async () => {
    await start(small);
    for (let i = 0; i < 3; i++) expect((await get(`/site/v/${V1}/%2e%2e/x${i}`, from('10.0.0.4'))).status).toBe(404);
    expect((await get(`/site/v/${V1}/`, from('10.0.0.4'))).status).toBe(429);
  });

  it('never counts an authenticated share 404 or an app 404', async () => {
    await start(small);
    for (let i = 0; i < 10; i++) {
      expect((await get(`/s/${TOKEN}/missing-${i}.png`, from('10.0.0.5', GOOD()))).status).toBe(404);
      expect((await get(`/site/v/${V1}/missing-${i}.png`, from('10.0.0.5'))).status).toBe(404);
    }
    expect((await get(`/site/v/${V1}/`, from('10.0.0.5'))).status).toBe(200);
  });

  it('limits all traffic of one credential, keyed by its hash', async () => {
    await start({ limits: { authFailuresPerAddress: 100, requestsPerCredential: 2 } });
    expect((await get(`/s/${TOKEN}/`, from('10.0.0.6', GOOD()))).status).toBe(200);
    expect((await get(`/s/${TOKEN}/`, from('10.0.0.7', GOOD()))).status).toBe(200);
    expect((await get(`/s/${TOKEN}/`, from('10.0.0.8', GOOD()))).status).toBe(429);
  });

  it('keeps answering 503 first when the module is disabled', async () => {
    await writeFile(path.join(etcDir, 'modules.json'), JSON.stringify({ versioned_artifacts: false }));
    await start(small);
    for (let i = 0; i < 5; i++) expect((await get(`/s/${TOKEN}/`, from('10.0.0.9'))).status).toBe(503);
  });
});
