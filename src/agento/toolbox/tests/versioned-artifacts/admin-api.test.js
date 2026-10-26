import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, writeFile, readFile } from 'node:fs/promises';
import fs from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { register } from '../../../modules/versioned_artifacts/toolbox/admin-api.js';
import { createService } from '../../../modules/versioned_artifacts/toolbox/service.js';
import { createBackend } from '../../../modules/versioned_artifacts/toolbox/git-backend.js';
import { ADMIN_SUBJECT, mintVersion, readSource, startAdminApp, vaConfig } from './helpers.js';

// The store admin routes under /admin (plan §2.1), behind the stand-in guard of startAdminApp.
const SUBJECT = ADMIN_SUBJECT;

let root, pub, src, server, base;
const cfg = () => vaConfig(root, pub, { allowed_artifacts: '' });

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'va-adm-store-'));
  pub = await mkdtemp(path.join(tmpdir(), 'va-adm-pub-'));
  src = await mkdtemp(path.join(tmpdir(), 'va-adm-src-'));
  await writeFile(path.join(src, 'index.html'), '<h1>v1</h1>\n');
  await createService({ config: cfg(), agentViewId: 7 }).init('site', { files: await readSource(src) });
  server = await startAdminApp(register, cfg(), { retentionPins: [] });
  base = `${server.origin}/admin/versioned-artifacts`;
});
afterEach(async () => {
  await server.close();
  for (const d of [root, pub, src]) await rm(d, { recursive: true, force: true });
});

const call = async (method, url, body) => {
  const r = await fetch(`${base}${url}`, {
    method, headers: body ? { 'content-type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined,
  });
  return { status: r.status, body: await r.json() };
};
const current = async () => (await createService({ config: cfg(), admin: true }).getCurrent('site')).current_version;

describe('versioned_artifacts admin api', () => {
  it('lists every artifact of the store, with no scope grant', async () => {
    const r = await call('GET', '');
    expect(r.status).toBe(200);
    expect(r.body.artifacts.map((a) => [a.artifact_code, a.current_version])).toEqual([['site', await current()]]);
  });

  it('lists the versions of one artifact, and answers 404 for an unknown one', async () => {
    const r = await call('GET', '/site/versions');
    expect(r.status).toBe(200);
    expect(r.body.versions.map((v) => v.version_id)).toEqual([await current()]);
    expect((await call('GET', '/nope/versions')).status).toBe(404);
    expect((await call('GET', '/Bad_Code/versions')).status).toBe(400);
  });

  it('publishes with a CAS: a stale expected version is 409 with the real current', async () => {
    const v1 = await current();
    const v2 = await mintVersion(createBackend(), root, 'site', '<h1>v2</h1>\n');
    const ok = await call('POST', '/site/publish', { version_id: v2, expected_current_version: v1 });
    expect(ok.status).toBe(200);
    expect(ok.body.current_version).toBe(v2);
    const stale = await call('POST', '/site/publish', { version_id: v1, expected_current_version: v1 });
    expect(stale.status).toBe(409);
    expect(stale.body).toMatchObject({ error_code: 'CURRENT_VERSION_CHANGED', current_version: v2 });
  });

  it('records the capability subject as the actor, never a body field', async () => {
    const v1 = await current();
    const v2 = await mintVersion(createBackend(), root, 'site', '<h1>v2</h1>\n');
    await call('POST', '/site/publish', { version_id: v2, expected_current_version: v1, actor: 'evil' });
    const rows = (await readFile(path.join(root, 'audit-fallback.log'), 'utf8')).trim().split('\n').map(JSON.parse);
    expect(rows.at(-1)).toMatchObject({ operation: 'versioned_artifact.version.published', actor: SUBJECT });
  });

  it('deletes an artifact; a second delete is 404', async () => {
    expect((await call('DELETE', '/site')).body).toMatchObject({ artifact_code: 'site', removed_store: true });
    expect(fs.existsSync(path.join(root, 'site'))).toBe(false);
    expect((await call('DELETE', '/site')).status).toBe(404);
  });

  it('sets and disables share auth', async () => {
    const on = await call('POST', '/site/auth', { user: 'viewer', password: 'pw-123456' });
    expect(on.status).toBe(200);
    expect(on.body).toMatchObject({ auth_enabled: true, auth_user: 'viewer', password: 'pw-123456' });
    expect((await call('POST', '/site/auth', { disable: true })).body).toMatchObject({ auth_enabled: false });
  });

  it('refuses a view-bound capability even if a mount mistake lets one through', async () => {
    server.capability = { kind: 'internal_rest', subject_id: 'service:consumer', agent_view_id: 7 };
    expect((await call('GET', '')).status).toBe(403);
    expect((await call('DELETE', '/site')).status).toBe(403);
    expect(fs.existsSync(path.join(root, 'site'))).toBe(true);
  });
});
