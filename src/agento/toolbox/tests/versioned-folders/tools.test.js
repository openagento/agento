import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, mkdir, writeFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { register } from '../../../modules/versioned_folders/toolbox/versioned-folders.js';
import { createService } from '../../../modules/versioned_folders/toolbox/service.js';
import { readSource } from './helpers.js';

function fakeServer() {
  const tools = new Map();
  return { tools, tool: (name, desc, schema, handler) => tools.set(name, { desc, schema, handler }) };
}

// The SAME complete value set Task 7's `cfg()` uses. An abbreviated fixture is
// not a smaller version of this one: `createService` validates every limit and
// `security/allow_symlinks` up front (Task 7), so a fixture missing them makes
// construction throw — and register() is fail-closed, so every test below would
// then assert against zero registered tools and "pass" for the wrong reason.
// storage_root is a path that cannot exist on a running toolbox — every test that
// lets the sweep RUN overrides it with a temp dir it owns. The production default
// (`/srv/versioned-folders`) must never appear in a fixture: the sweep deletes lock
// directories under whatever root it is handed, and on the toolbox container that
// path is the live store. One forgotten override would be enough.
const CONFIG = {
  storage_root: '/nonexistent/vf-test-root', allowed_folders: 'site',
  'limits/max_files': 2000, 'limits/max_file_size': 5242880,
  'limits/max_total_size': 104857600, 'limits/max_diff_bytes': 1048576,
  'security/allow_symlinks': false,
};
const baseCtx = (over = {}) => ({
  log: vi.fn(),
  moduleConfigs: { versioned_folders: { ...CONFIG, ...(over.config ?? {}) } },
  db: null, jobId: null, agentViewId: null, ...over,
});

const ALL = [
  'versioned_folder_get_current', 'versioned_folder_create_draft', 'versioned_folder_list_files',
  'versioned_folder_read_file', 'versioned_folder_apply_changes', 'versioned_folder_diff',
  'versioned_folder_finalize', 'versioned_folder_publish', 'versioned_folder_list_versions',
  'versioned_folder_discard_draft',
];

it('registers all ten tools in the stub pass', async () => {
  const s = fakeServer(); await register(s, baseCtx());
  expect([...s.tools.keys()].sort()).toEqual([...ALL].sort());
});

it('registers nothing when the gate denies everything', async () => {
  const s = fakeServer(); await register(s, baseCtx({ isToolEnabled: () => false }));
  expect(s.tools.size).toBe(0);
});

it('registers only the enabled tool', async () => {
  const s = fakeServer(); await register(s, baseCtx({ isToolEnabled: (n) => n === 'versioned_folder_diff' }));
  expect([...s.tools.keys()]).toEqual(['versioned_folder_diff']);
});

it('sweeps stale locks in the STARTUP pass only', async () => {
  // `app` present == registerModuleRestApis; absent == an MCP session. The
  // sweep deletes lock directories, so running it per session would break a
  // live holder in another process.
  //
  // The storage_root is a TEMP DIR the test creates and removes.
  // This test's subject is a function that deletes directories under the root it
  // is given; on any host where that path is the real store — the toolbox
  // container itself — the test would sweep production locks. A test that
  // mutates the filesystem must own the filesystem it mutates.
  const sweepRoot = await mkdtemp(path.join(tmpdir(), 'vf-sweep-'));
  const app = { post: vi.fn(), get: vi.fn(), use: vi.fn() };
  const log = vi.fn();
  await register(fakeServer(), baseCtx({ app, log, config: { storage_root: sweepRoot } }));
  expect(log).toHaveBeenCalledWith('versioned_folders', 'OK', expect.stringMatching(/swept \d+ stale lock/));

  log.mockClear();
  await register(fakeServer(), baseCtx({ log, config: { storage_root: sweepRoot } }));   // app: undefined
  expect(log).not.toHaveBeenCalledWith('versioned_folders', 'OK', expect.stringMatching(/swept/));
  await rm(sweepRoot, { recursive: true, force: true });
});

it('registers every tool even when the store does not exist yet', async () => {
  // First boot after install: the volume is empty, so the sweep finds nothing
  // and must not be able to take registration down with it.
  const log = vi.fn();
  const s = fakeServer();
  await register(s, baseCtx({ app: {}, log, config: { storage_root: '/nonexistent/vf-other-root' } }));
  expect(s.tools.size).toBe(10);
  expect(log).not.toHaveBeenCalledWith('versioned_folders', 'ERROR', expect.anything());
});

it('exposes no git vocabulary in any tool name or parameter', async () => {
  const s = fakeServer(); await register(s, baseCtx());
  const banned = /repo(sitory)?|branch|commit|merge|rebase|checkout|worktree|\bref\b|\bgit\b/i;
  for (const [name, { schema }] of s.tools) {
    expect(name).not.toMatch(banned);
    for (const param of Object.keys(schema)) expect(param).not.toMatch(banned);
  }
});

it('exposes no tool for the admin-only or internal operations', async () => {
  const s = fakeServer(); await register(s, baseCtx());
  expect(s.tools.has('versioned_folder_init')).toBe(false);
  expect(s.tools.has('versioned_folder_get_draft_path')).toBe(false);
});

it('requires expected_current_version on publish', async () => {
  const s = fakeServer(); await register(s, baseCtx());
  const schema = s.tools.get('versioned_folder_publish').schema;
  expect(schema).toHaveProperty('expected_current_version');
  expect(schema.expected_current_version.isOptional()).toBe(false);
});

it('denies a folder outside the allowlist', async () => {
  const s = fakeServer(); await register(s, baseCtx());
  const res = await s.tools.get('versioned_folder_get_current').handler({ folder_code: 'not-allowed', user: 'a@b.c' });
  expect(JSON.stringify(res)).toContain('FOLDER_ACCESS_DENIED');
});

it('registers nothing and logs when the configuration is invalid', async () => {
  // Fail closed: a service that cannot be constructed must not leave a partial
  // toolset registered, and the reason must reach the operator's log — not the
  // model. `max_files: 0` is rejected by Task 7's parser.
  const log = vi.fn();
  const s = fakeServer();
  await register(s, baseCtx({ log, config: { 'limits/max_files': 0 } }));
  expect(s.tools.size).toBe(0);
  expect(log).toHaveBeenCalledWith('versioned_folders', 'ERROR', expect.anything());
});

// --- success-path contract tests -------------------------------------------
// Every failure-path test above is satisfied by an adapter that reshapes the
// service result wrongly, because the error never travels through the reshape.
// These run against a REAL store, and they are the only tests that pin the
// PRD §22 output shape of the adapters that do not pass their result through
// unchanged: list_files wraps an array, list_versions wraps an array AND adds
// current_version, get_current echoes folder_code back.
//
// Scoped inside a describe: a file-level beforeEach would build a Git store for
// every registration test above, which needs none of it.
describe('against a real store', () => {
let root, src;
beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'vf-tools-'));
  src = await mkdtemp(path.join(tmpdir(), 'vf-tools-src-'));
  await writeFile(path.join(src, 'index.html'), '<h1>v1</h1>\n');
  // Built through the service, not the backend: tools.test.js must not import
  // git-backend.js — that is exactly the edge Task 9's layering guard forbids.
  const admin = createService({ config: { ...CONFIG, storage_root: root }, db: null,
    log: vi.fn(), actor: 'admin' });
  await admin.init('site', { files: await readSource(src) });
});
afterEach(async () => { await rm(root, {recursive:true,force:true}); await rm(src, {recursive:true,force:true}); });

const payload = (res) => JSON.parse(res.content[0].text);
const liveTools = async () => {
  const s = fakeServer();
  await register(s, baseCtx({ config: { storage_root: root } }));
  return s.tools;
};

it('returns the PRD §22.1 shape from get_current', async () => {
  const tools = await liveTools();
  const out = payload(await tools.get('versioned_folder_get_current').handler({ folder_code: 'site', user: 'a@b.c' }));
  expect(Object.keys(out).sort()).toEqual(['current_version', 'folder_code']);
  expect(out.folder_code).toBe('site');
  expect(out.current_version).toMatch(/^v-\d{8}-\d{6}-[a-z0-9]{4}$/);
});

it('returns the PRD §22.3 shape from list_files', async () => {
  const tools = await liveTools();
  const cur = payload(await tools.get('versioned_folder_get_current').handler({ folder_code: 'site', user: 'a@b.c' }));
  const out = payload(await tools.get('versioned_folder_list_files')
    .handler({ folder_code: 'site', version_id: cur.current_version, user: 'a@b.c' }));
  expect(Array.isArray(out.files)).toBe(true);                 // wrapped, not a bare array
  expect(out.files.map(f => f.path)).toContain('index.html');
  expect(typeof out.files[0].size).toBe('number');
});

it('returns the PRD §22.9 shape from list_versions, including current_version', async () => {
  const tools = await liveTools();
  const out = payload(await tools.get('versioned_folder_list_versions').handler({ folder_code: 'site', limit: 50, user: 'a@b.c' }));
  expect(Array.isArray(out.versions)).toBe(true);
  // current_version comes from a SECOND service call; an adapter that forwards
  // only listVersions() drops it, and no failure-path test can see that.
  // Membership, not position — same reason as the finalize-retry test: the listing is
  // ordered by the version id, whose timestamp has one-second resolution, so index 0
  // is not a stable identity even when the store happens to hold one version today.
  expect(out.versions.map(v => v.version_id)).toContain(out.current_version);
  for (const v of out.versions) expect(Object.keys(v).sort()).toEqual(['revision', 'version_id']);
});

it('audits the REQUEST user, through the handler that has to pass it', async () => {
  // Task 7 proves forActor() works; this proves the adapter calls it. The defect
  // was never in the service — it was in the registrar, which builds the service
  // once and holds no user. A service-level test cannot see that, and this is the
  // only layer where `args.user` and the audit row meet.
  const rows = [];
  const s = fakeServer();
  await register(s, baseCtx({ config: { storage_root: root },
    db: { getCronPool: () => ({ execute: async (_sql, params) => { rows.push(params); } }) } }));
  await s.tools.get('versioned_folder_create_draft').handler(
    { folder_code: 'site', base_version: 'current', description: 'x', user: 'requester@b.c' });
  expect(rows.at(-1)).toContain('requester@b.c');
});

it('never leaks a host stack trace to the model', async () => {
  const s = fakeServer();
  await register(s, baseCtx({ config: { storage_root: '/nonexistent/vf-other-root' } }));
  const res = await s.tools.get('versioned_folder_get_current').handler({ folder_code: 'site', user: 'a@b.c' });
  const text = JSON.stringify(res);
  expect(text).not.toMatch(/vf-other-root|at Object\.|node:internal|ENOENT:|fatal:/);
  expect(text).toMatch(/FOLDER_NOT_FOUND|GIT_OPERATION_FAILED/);
});

// The reclamation half of startupSweep: it needs a store an orphan can exist
// in, so the fake-config sweep test above cannot reach it.
it('reclaims an incomplete draft during the STARTUP pass', async () => {
  // A crash-interrupted creation: a draft directory with no base ref. Nothing
  // knows its id, so only the sweep can ever remove it.
  const orphan = path.join(root, 'site', 'worktrees', 'd-deadbe');
  await mkdir(orphan, { recursive: true });
  const log = vi.fn();
  await register(fakeServer(), baseCtx({ app: {}, log, config: { storage_root: root } }));
  await expect(stat(orphan)).rejects.toThrow();               // reclaimed
  expect(log).toHaveBeenCalledWith('versioned_folders', 'OK', expect.stringMatching(/reclaimed 1 incomplete draft/));
  expect(log).not.toHaveBeenCalledWith('versioned_folders', 'ERROR', expect.anything());
});
});   // describe('against a real store')
