import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { mkdtemp, rm, readFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createService } from '../../../modules/versioned_artifacts/toolbox/service.js';
import { verifyCredential } from '../../../modules/versioned_artifacts/toolbox/auth.js';

// A reversible stand-in for the framework AES helper — the service only needs a pair
// that round-trips; the real one is exercised by the config-loader suite.
const fakeCrypto = {
  encrypt: (s) => `enc:${Buffer.from(String(s), 'utf8').toString('base64')}`,
  decrypt: (s) => Buffer.from(String(s).slice(4), 'base64').toString('utf8'),
};

// An in-memory `versioned_artifact` table: enough of MySQL's INSERT/upsert/SELECT for
// the auth round trip, ignoring the audit INSERTs that share the same pool.
function fakeDb() {
  const table = new Map();
  const upsert = (code, patch) => table.set(code, { ...(table.get(code) || { artifact_code: code }), ...patch });
  const pool = {
    execute: async (sql, params) => {
      if (/INSERT INTO versioned_artifact \(artifact_code, title, owner\)/.test(sql)) {
        upsert(params[0], { title: params[1], owner: params[2] });
      } else if (/auth_enabled = 1/.test(sql)) {
        upsert(params[0], { auth_enabled: 1, auth_user: params[1], auth_secret_enc: params[2] });
      } else if (/auth_enabled = 0/.test(sql)) {
        upsert(params[0], { auth_enabled: 0, auth_user: null, auth_secret_enc: null });
      } else if (/DELETE FROM versioned_artifact/.test(sql)) {
        table.delete(params[0]);
      }
    },
    query: async (sql, params) => {
      if (/SELECT auth_enabled/.test(sql)) { const r = table.get(params[0]); return [r ? [r] : []]; }
      if (/SELECT artifact_code, title/.test(sql)) return [[...table.values()]];
      return [[]];
    },
  };
  return { getCronPool: () => pool, table };
}

let root; let pub; let db;
const files = [{ path: 'index.html', content: Buffer.from('<h1>v1</h1>').toString('base64'), encoding: 'base64' }];
const cfg = (over = {}) => ({ storage_root: root, published_root: pub, allowed_artifacts: 'site',
  'serving/keep_versions': 0, 'serving/public_base_url': 'http://localhost:8080', 'limits/max_files': 2000,
  'limits/max_file_size': 5242880, 'limits/max_total_size': 104857600, 'limits/max_diff_bytes': 1048576,
  'limits/max_agent_artifacts': 50, 'security/allow_symlinks': false, ...over });
const svc = (over = {}, opts = {}) => createService({ config: cfg(over), db, log: vi.fn(),
  actor: 'a@b.c', crypto: fakeCrypto, ...opts });
const sidecar = async () => JSON.parse(await readFile(path.join(pub, 'site', '.auth'), 'utf8'));

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'vf-auth-'));
  pub = await mkdtemp(path.join(tmpdir(), 'vf-auth-pub-'));
  db = fakeDb();
});
afterEach(async () => {
  await rm(root, { recursive: true, force: true });
  await rm(pub, { recursive: true, force: true });
});

describe('init auto-auth', () => {
  it('does nothing when basic_auth_default is off', async () => {
    const r = await svc().init('site', { files });
    expect(r.basic_auth).toBeUndefined();
    await expect(stat(path.join(pub, 'site', '.auth'))).rejects.toThrow();
  });

  it('returns credentials and writes an enforceable sidecar when default is on', async () => {
    const r = await svc({ 'security/basic_auth_default': true }).init('site', { files });
    expect(r.basic_auth.user).toBe('site');
    expect(r.basic_auth.password).toMatch(/\S{20,}/);
    const s = await sidecar();
    expect(verifyCredential(s, r.basic_auth.user, r.basic_auth.password)).toBe(true);
    expect(JSON.stringify(s)).not.toContain(r.basic_auth.password);   // hash only, never plaintext
    expect(db.table.get('site').auth_enabled).toBe(1);
    expect(db.table.get('site').auth_secret_enc).toMatch(/^enc:/);
  });

  it('creates the artifact but returns no credential when the key is missing', async () => {
    const log = vi.fn();
    const r = await svc({ 'security/basic_auth_default': true }, { crypto: null, log }).init('site', { files });
    expect(r.current_version).toMatch(/^v-/);
    expect(r.basic_auth).toBeUndefined();
    await expect(stat(path.join(pub, 'site', '.auth'))).rejects.toThrow();
    expect(log).toHaveBeenCalledWith('versioned_artifacts', 'ERROR', expect.stringMatching(/basic auth not enabled/));
  });
});

describe('setAuth / getAuth', () => {
  beforeEach(async () => { await svc().init('site', { files }); });

  it('sets an explicit credential, then reads it back decrypted', async () => {
    const set = await svc().setAuth('site', { user: 'admin', password: 'hunter2' });
    expect(set).toMatchObject({ auth_enabled: true, auth_user: 'admin', password: 'hunter2' });
    expect(verifyCredential(await sidecar(), 'admin', 'hunter2')).toBe(true);
    const shown = await svc().getAuth('site');
    expect(shown).toMatchObject({ auth_enabled: true, auth_user: 'admin', password: 'hunter2' });
  });

  it('fills an empty user and password with the code and a strong random one', async () => {
    const set = await svc().setAuth('site', {});
    expect(set.auth_user).toBe('site');
    expect(set.password).toMatch(/\S{20,}/);
    expect(verifyCredential(await sidecar(), 'site', set.password)).toBe(true);
  });

  it('disable removes the sidecar and reports auth off', async () => {
    await svc().setAuth('site', { user: 'admin', password: 'hunter2' });
    const off = await svc().setAuth('site', { disable: true });
    expect(off).toMatchObject({ auth_enabled: false });
    await expect(stat(path.join(pub, 'site', '.auth'))).rejects.toThrow();
    expect(await svc().getAuth('site')).toMatchObject({ auth_enabled: false });
  });

  it('refuses when no encryption key is configured', async () => {
    await expect(svc({}, { crypto: null }).setAuth('site', { password: 'x' }))
      .rejects.toThrow(/AUTH_UNAVAILABLE/);
  });

  it('audits the change as versioned_artifact.auth.set', async () => {
    const rows = [];
    const auditingDb = { getCronPool: () => ({
      execute: async (sql, params) => { if (/versioned_artifact_audit/.test(sql)) rows.push(params); await db.getCronPool().execute(sql, params); },
      query: (...a) => db.getCronPool().query(...a),
    }) };
    const s = createService({ config: cfg(), db: auditingDb, log: vi.fn(), actor: 'a@b.c', crypto: fakeCrypto });
    await s.setAuth('site', { user: 'admin', password: 'hunter2' });
    expect(rows.at(-1)).toContain('versioned_artifact.auth.set');
  });
});
