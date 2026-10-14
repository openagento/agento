import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import express from 'express';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import fs from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createModuleRouteApp } from '../../config-loader.js';
import { register } from '../../../modules/miniapps/toolbox/admin-api.js';
import { createService } from '../../../modules/versioned_artifacts/toolbox/service.js';
import { createBackend } from '../../../modules/versioned_artifacts/toolbox/git-backend.js';
import { mintVersion, readSource } from '../versioned-artifacts/helpers.js';

// POST /admin/miniapps/<code>/resolve (plan §2.4): a version (or `current`) → its served
// tree, its content id and its manifest. web approves and launches from this answer.
const MANIFEST = { schema: 1, title: 'Demo', actions: ['jira_search'] };

let root, pub, src, bare, listener, base, capability;
const cfg = (over = {}) => ({ storage_root: root, published_root: pub, allowed_artifacts: '',
  'limits/max_files': 2000, 'limits/max_file_size': 5242880, 'limits/max_total_size': 104857600,
  'limits/max_diff_bytes': 1048576, 'limits/max_agent_artifacts': 50, 'security/allow_symlinks': false,
  'serving/keep_versions': 1, 'serving/public_base_url': 'http://localhost:8080', ...over });
const admin = () => createService({ config: cfg(), admin: true });
// The agent creates artifacts; the store admin does not (D-MINIAPP-2, O-3).
const agent = () => createService({ config: cfg(), agentViewId: 7 });

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'ma-adm-store-'));
  pub = await mkdtemp(path.join(tmpdir(), 'ma-adm-pub-'));
  src = await mkdtemp(path.join(tmpdir(), 'ma-adm-src-'));
  bare = await mkdtemp(path.join(tmpdir(), 'ma-adm-bare-'));
  await writeFile(path.join(src, 'index.html'), '<h1>v1</h1>\n');
  await writeFile(path.join(src, 'miniapp.json'), JSON.stringify(MANIFEST));
  await writeFile(path.join(bare, 'index.html'), '<h1>no manifest</h1>\n');
  await agent().init('app', { files: await readSource(src) });
  await agent().init('plain', { files: await readSource(bare) });
  capability = { kind: 'internal_rest', subject_id: 'service:web:user:3', agent_view_id: null };
  const app = express();
  app.use((req, _res, next) => { req.capability = capability; next(); }, express.json());
  await register({ tool: () => {} }, {
    app: createModuleRouteApp(app, vi.fn()), log: vi.fn(), db: null, moduleConfigs: { versioned_artifacts: cfg() },
  });
  listener = await new Promise((resolve) => { const l = app.listen(0, '127.0.0.1', () => resolve(l)); });
  base = `http://127.0.0.1:${listener.address().port}/admin/miniapps`;
});
afterEach(async () => {
  await new Promise((resolve) => listener.close(resolve));
  for (const d of [root, pub, src, bare]) await rm(d, { recursive: true, force: true });
});

const resolve = async (code, version) => {
  const r = await fetch(`${base}/${code}/resolve`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ version }),
  });
  return { status: r.status, body: await r.json() };
};
const vDir = (code, id) => path.join(pub, code, 'v', id);

describe('miniapps admin resolve', () => {
  it('resolves current to its version, content id and manifest', async () => {
    const v1 = (await admin().getCurrent('app')).current_version;
    const r = await resolve('app', 'current');
    expect(r.status).toBe(200);
    expect(r.body).toEqual({ version_id: v1, content_id: expect.stringMatching(/^[0-9a-f]{40}$/),
      manifest: { title: 'Demo', actions: ['jira_search'] } });
    expect(r.body.content_id).toBe(await admin().versionContentId('app', v1));
  });

  it('serves the tree of a version retention pruned', async () => {
    const v1 = (await admin().getCurrent('app')).current_version;
    const v2 = await mintVersion(createBackend(), root, 'app', '<h1>v2</h1>\n');
    await admin().publish('app', v2, v1);
    expect(fs.existsSync(vDir('app', v1))).toBe(false);
    const r = await resolve('app', v1);
    expect(r.body.version_id).toBe(v1);
    expect(fs.existsSync(path.join(vDir('app', v1), 'index.html'))).toBe(true);
  });

  // Plan §2.4: approve after a failed `current`-pointer swap. The store moved to v2, the
  // served pointer did not; approve resolves the STORE's current and serves its tree.
  it('resolves the store current and serves its tree after a failed pointer swap', async () => {
    const v1 = (await admin().getCurrent('app')).current_version;
    const v2 = await mintVersion(createBackend(), root, 'app', '<h1>v2</h1>\n');
    const link = path.join(pub, 'app', 'current');
    await rm(link, { force: true });
    await mkdir(link, { recursive: true });   // a directory at `current`: the swap fails (EISDIR)
    expect((await admin().publish('app', v2, v1)).preview_stale).toBe(true);
    await rm(vDir('app', v2), { recursive: true, force: true });   // and its tree is gone
    const r = await resolve('app', 'current');
    expect(r.body.version_id).toBe(v2);
    expect(fs.readFileSync(path.join(vDir('app', v2), 'index.html'), 'utf8')).toBe('<h1>v2</h1>\n');
  });

  it('answers a null manifest when the version has none', async () => {
    expect((await resolve('plain', 'current')).body.manifest).toBeNull();
  });

  it('answers 404 for an unknown version or artifact, 400 for a malformed version', async () => {
    expect((await resolve('app', 'v-20990101-000000-dead')).status).toBe(404);
    expect((await resolve('nope', 'current')).status).toBe(404);
    expect((await resolve('app', '../x')).status).toBe(400);
    expect((await resolve('app', undefined)).status).toBe(400);
  });

  it('refuses a view-bound capability', async () => {
    capability = { kind: 'internal_rest', subject_id: 'service:consumer', agent_view_id: 7 };
    expect((await resolve('app', 'current')).status).toBe(403);
  });

  // Plan §2.4: the approval pins the content, not the version id, because a re-created
  // artifact can reuse an id. Web compares this content_id with the pinned one.
  it('gives a re-created artifact the old content id only for the same bytes', async () => {
    const before = (await resolve('app', 'current')).body.content_id;
    const recreate = async (dir) => {
      await admin().remove('app');
      await agent().init('app', { files: await readSource(dir) });
      return (await resolve('app', 'current')).body.content_id;
    };
    await writeFile(path.join(bare, 'miniapp.json'), JSON.stringify(MANIFEST));
    expect(await recreate(bare)).not.toBe(before);
    expect(await recreate(src)).toBe(before);
  });
});
