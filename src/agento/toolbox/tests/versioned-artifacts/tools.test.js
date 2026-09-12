import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, mkdir, writeFile, readFile, readdir, stat } from 'node:fs/promises';
import fs from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { register } from '../../../modules/versioned_artifacts/toolbox/versioned-artifacts.js';
import { createService } from '../../../modules/versioned_artifacts/toolbox/service.js';
import { ARTIFACTS_ROOT } from '../../../modules/versioned_artifacts/toolbox/desk-io.js';

// Read, not imported: an import attribute (`with { type: 'json' }`) is newer than the
// repo's ESLint parser, and this file has to lint.
const manifest = JSON.parse(
  fs.readFileSync(new URL('../../../modules/versioned_artifacts/module.json', import.meta.url), 'utf8'));
import { readSource } from './helpers.js';

// The desk-taking tools walk the real `/workspace/artifacts` mount through fd-anchored
// syscalls, so their handlers are Linux-only; registration and schemas are not.
const linux = process.platform === 'linux';

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
// (`/srv/versioned-artifacts`) must never appear in a fixture: the sweep deletes lock
// directories under whatever root it is handed, and on the toolbox container that
// path is the live store. One forgotten override would be enough.
const CONFIG = {
  storage_root: '/nonexistent/artifact-test-root', published_root: '/nonexistent/artifact-test-published',
  allowed_artifacts: 'site', 'serving/keep_versions': 0, 'serving/public_base_url': 'http://localhost:8080',
  'limits/max_files': 2000, 'limits/max_file_size': 5242880,
  'limits/max_total_size': 104857600, 'limits/max_diff_bytes': 1048576,
  'security/allow_symlinks': false,
};
const baseCtx = (over = {}) => ({
  log: vi.fn(),
  moduleConfigs: { versioned_artifacts: { ...CONFIG, ...(over.config ?? {}) } },
  db: null, jobId: null, agentViewId: null, ...over,
});

const ALL = [
  'versioned_artifact_list', 'versioned_artifact_get_current', 'versioned_artifact_list_versions',
  'versioned_artifact_create_draft', 'versioned_artifact_materialize', 'versioned_artifact_save_version',
  'versioned_artifact_diff', 'versioned_artifact_publish', 'versioned_artifact_discard_draft',
];

it('registers all nine tools in the stub pass', async () => {
  const s = fakeServer(); await register(s, baseCtx());
  expect([...s.tools.keys()].sort()).toEqual([...ALL].sort());
});

it('declares exactly ten names — the toolset switch and the nine tools it gates', async () => {
  // The manifest is the allow-list the framework denies against, so a tool that lost
  // its declaration is denied at runtime and one that kept it after deletion is a
  // name nothing can ever enable. Both are only visible from here.
  const declared = manifest.tools.map((t) => t.name);
  expect(declared.sort()).toEqual(['versioned_artifact', ...ALL].sort());
  for (const t of manifest.tools.filter((t) => t.name !== 'versioned_artifact')) {
    expect(t.requires).toBe('versioned_artifact');
  }
});

it('registers nothing when the gate denies everything', async () => {
  const s = fakeServer(); await register(s, baseCtx({ isToolEnabled: () => false }));
  expect(s.tools.size).toBe(0);
});

it('registers only the enabled tool', async () => {
  const s = fakeServer(); await register(s, baseCtx({ isToolEnabled: (n) => n === 'versioned_artifact_diff' }));
  expect([...s.tools.keys()]).toEqual(['versioned_artifact_diff']);
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
  expect(log).toHaveBeenCalledWith('versioned_artifacts', 'OK', expect.stringMatching(/swept \d+ stale lock/));

  log.mockClear();
  await register(fakeServer(), baseCtx({ log, config: { storage_root: sweepRoot } }));   // app: undefined
  expect(log).not.toHaveBeenCalledWith('versioned_artifacts', 'OK', expect.stringMatching(/swept/));
  await rm(sweepRoot, { recursive: true, force: true });
});

it('registers every tool even when the store does not exist yet', async () => {
  // First boot after install: the volume is empty, so the sweep finds nothing
  // and must not be able to take registration down with it.
  const log = vi.fn();
  const s = fakeServer();
  await register(s, baseCtx({ app: {}, log, config: { storage_root: '/nonexistent/vf-other-root' } }));
  expect(s.tools.size).toBe(9);
  expect(log).not.toHaveBeenCalledWith('versioned_artifacts', 'ERROR', expect.anything());
});

it('exposes no git vocabulary in any tool name, parameter or DESCRIPTION', async () => {
  // The description is the half the model actually reads. A tool named
  // `save_version` that describes itself as "commit the draft" leaks the storage
  // engine just as effectively as the name would have.
  const s = fakeServer(); await register(s, baseCtx());
  const banned = /repo(sitory)?|branch|commit|merge|rebase|checkout|worktree|\btag\b|\bref\b|\bgit\b/i;
  for (const [name, { desc, schema }] of s.tools) {
    expect(name).not.toMatch(banned);
    expect(desc).not.toMatch(banned);
    for (const [param, shape] of Object.entries(schema)) {
      expect(param).not.toMatch(banned);
      expect(shape.description ?? '').not.toMatch(banned);
    }
  }
  for (const t of manifest.tools) expect(t.description).not.toMatch(banned);
});

it('takes no user and no filesystem path in any schema', async () => {
  // The MCP session identifies the caller, and the desk is derived server-side —
  // a tool that accepts either has re-opened a hole this phase closed.
  const s = fakeServer(); await register(s, baseCtx());
  const pathShaped = /^(path|dir|directory|file|filename|dest|destination|target|location)$/i;
  for (const [, { schema }] of s.tools) {
    for (const param of Object.keys(schema)) {
      expect(param).not.toBe('user');
      expect(param).not.toMatch(pathShaped);
    }
  }
});

it('exposes no tool for the admin-only or internal operations', async () => {
  const s = fakeServer(); await register(s, baseCtx());
  expect(s.tools.has('versioned_artifact_init')).toBe(false);
  expect(s.tools.has('versioned_artifact_get_draft_path')).toBe(false);
});

it('requires expected_current_version on publish', async () => {
  const s = fakeServer(); await register(s, baseCtx());
  const schema = s.tools.get('versioned_artifact_publish').schema;
  expect(schema).toHaveProperty('expected_current_version');
  expect(schema.expected_current_version.isOptional()).toBe(false);
});

it('denies an artifact outside the allowlist', async () => {
  const s = fakeServer(); await register(s, baseCtx());
  const res = await s.tools.get('versioned_artifact_get_current').handler({ artifact_code: 'not-allowed' });
  expect(JSON.stringify(res)).toContain('ARTIFACT_ACCESS_DENIED');
});

it('registers nothing and logs when the configuration is invalid', async () => {
  // Fail closed: a service that cannot be constructed must not leave a partial
  // toolset registered, and the reason must reach the operator's log — not the
  // model. `max_files: 0` is rejected by Task 7's parser.
  const log = vi.fn();
  const s = fakeServer();
  await register(s, baseCtx({ log, config: { 'limits/max_files': 0 } }));
  expect(s.tools.size).toBe(0);
  expect(log).toHaveBeenCalledWith('versioned_artifacts', 'ERROR', expect.anything());
});

// --- success-path contract tests -------------------------------------------
// Every failure-path test above is satisfied by an adapter that reshapes the
// service result wrongly, because the error never travels through the reshape.
// These run against a REAL store, and they are the only tests that pin the
// PRD §22 output shape of the adapters that do not pass their result through
// unchanged: list_versions wraps an array AND adds current_version, `list` wraps
// one too, and get_current echoes artifact_code back.
//
// Scoped inside a describe: a file-level beforeEach would build a Git store for
// every registration test above, which needs none of it.
describe('against a real store', () => {
let root, src, session;
let seq = 0;
// A unique <ws>/<av>/<job> under the REAL mount point, so the desk-taking tools run
// against the production constant rather than an injected test root.
const newSession = () => {
  const id = `${process.pid}t${seq++}`;
  const dir = path.join(ARTIFACTS_ROOT, `ws${id}`, `av${id}`, `${id}`);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
};
beforeEach(async () => {
  session = linux ? newSession() : '/workspace/artifacts/ws/av/1';
  root = await mkdtemp(path.join(tmpdir(), 'vf-tools-'));
  src = await mkdtemp(path.join(tmpdir(), 'vf-tools-src-'));
  await writeFile(path.join(src, 'index.html'), '<h1>v1</h1>\n');
  // Built through the service, not the backend: tools.test.js must not import
  // git-backend.js — that is exactly the edge Task 9's layering guard forbids.
  const admin = createService({ config: { ...CONFIG, storage_root: root }, db: null,
    log: vi.fn(), actor: 'admin' });
  await admin.init('site', { files: await readSource(src) });
});
afterEach(async () => {
  await rm(root, {recursive:true,force:true});
  await rm(src, {recursive:true,force:true});
  if (linux) await rm(path.dirname(path.dirname(session)), {recursive:true,force:true});
});

const payload = (res) => JSON.parse(res.content[0].text);
const liveTools = async (over = {}) => {
  const s = fakeServer();
  await register(s, baseCtx({ config: { storage_root: root }, artifactsDir: session, ...over }));
  return s.tools;
};

it('returns the PRD §22.1 shape from get_current', async () => {
  const tools = await liveTools();
  const out = payload(await tools.get('versioned_artifact_get_current').handler({ artifact_code: 'site' }));
  expect(Object.keys(out).sort()).toEqual(['artifact_code', 'current_version', 'preview_url']);
  expect(out.preview_url).toBe('http://localhost:8080/site/');
  expect(out.artifact_code).toBe('site');
  expect(out.current_version).toMatch(/^v-\d{8}-\d{6}-[a-z0-9]{4}$/);
});

it('returns the artifacts this scope may use, wrapped, from list', async () => {
  const tools = await liveTools();
  const out = payload(await tools.get('versioned_artifact_list').handler({}));
  expect(Array.isArray(out.artifacts)).toBe(true);             // wrapped, not a bare array
  expect(out.artifacts.map(a => a.artifact_code)).toEqual(['site']);
  expect(out.artifacts[0].current_version).toMatch(/^v-\d{8}-\d{6}-[a-z0-9]{4}$/);
  expect(out.artifacts[0].open_drafts).toEqual([]);
});

it('returns the PRD §22.9 shape from list_versions, including current_version', async () => {
  const tools = await liveTools();
  const out = payload(await tools.get('versioned_artifact_list_versions').handler({ artifact_code: 'site', limit: 50 }));
  expect(Array.isArray(out.versions)).toBe(true);
  // current_version comes from a SECOND service call; an adapter that forwards
  // only listVersions() drops it, and no failure-path test can see that.
  // Membership, not position — same reason as the save-retry test: the listing is
  // ordered by the version id, whose timestamp has one-second resolution, so index 0
  // is not a stable identity even when the store happens to hold one version today.
  expect(out.versions.map(v => v.version_id)).toContain(out.current_version);
  for (const v of out.versions) expect(Object.keys(v).sort()).toEqual(['preview_path', 'revision', 'version_id']);
});

it('never leaks a host stack trace to the model', async () => {
  const s = fakeServer();
  await register(s, baseCtx({ config: { storage_root: '/nonexistent/vf-other-root' } }));
  const res = await s.tools.get('versioned_artifact_get_current').handler({ artifact_code: 'site' });
  const text = JSON.stringify(res);
  expect(text).not.toMatch(/vf-other-root|at Object\.|node:internal|ENOENT:|fatal:/);
  expect(text).toMatch(/ARTIFACT_NOT_FOUND|STORAGE_OPERATION_FAILED/);
});

// The admin exemption `cli.js` sets lives on `createService`, and this is the half of
// it that matters: the TOOL layer must never reach it. A behavioural assertion, not a
// text search — `register()` is executed and the agent's own call is denied.
it('denies every artifact to the tools when the scope allowlist is empty', async () => {
  const s = fakeServer();
  await register(s, baseCtx({ config: { storage_root: root, allowed_artifacts: '' } }));
  const res = await s.tools.get('versioned_artifact_get_current').handler({ artifact_code: 'site' });
  expect(payload(res).error_code).toBe('ARTIFACT_ACCESS_DENIED');
  const listed = payload(await s.tools.get('versioned_artifact_list').handler({}));
  expect(listed.artifacts).toEqual([]);
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
  expect(log).toHaveBeenCalledWith('versioned_artifacts', 'OK', expect.stringMatching(/reclaimed 1 incomplete draft/));
  expect(log).not.toHaveBeenCalledWith('versioned_artifacts', 'ERROR', expect.anything());
});
describe.skipIf(!linux)('the desk the tools hand the agent', () => {
  const deskOf = (code, id) => path.join(session, 'versioned-artifacts', code, id);

  it('creates a draft and leaves its files on the desk it names', async () => {
    const tools = await liveTools();
    const out = payload(await tools.get('versioned_artifact_create_draft')
      .handler({ artifact_code: 'site', base_version: 'current', description: 'x' }));
    expect(Object.keys(out).sort()).toEqual(['base_version', 'draft_id', 'path']);
    expect(out.path).toBe(deskOf('site', out.draft_id));
    expect(await readdir(out.path)).toEqual(['index.html']);
  });

  it('replaces whatever the desk already held', async () => {
    const tools = await liveTools();
    const cur = payload(await tools.get('versioned_artifact_get_current').handler({ artifact_code: 'site' }));
    const desk = deskOf('site', cur.current_version);
    await mkdir(path.join(desk, 'stale-dir'), { recursive: true });
    await writeFile(path.join(desk, 'stale.txt'), 'from a previous attempt');

    const out = payload(await tools.get('versioned_artifact_materialize')
      .handler({ artifact_code: 'site', version_id: cur.current_version }));

    expect(out.path).toBe(desk);
    expect(await readdir(desk)).toEqual(['index.html']);
  });

  it('saves what the agent wrote on the desk, and keeps the draft open', async () => {
    const tools = await liveTools();
    const d = payload(await tools.get('versioned_artifact_create_draft')
      .handler({ artifact_code: 'site', base_version: 'current', description: 'x' }));
    await writeFile(path.join(d.path, 'index.html'), '<h1>v2</h1>\n');

    const saved = payload(await tools.get('versioned_artifact_save_version')
      .handler({ artifact_code: 'site', draft_id: d.draft_id, description: 'second' }));

    expect(saved.version_id).toMatch(/^v-\d{8}-\d{6}-[a-z0-9]{4}$/);
    const [site] = payload(await tools.get('versioned_artifact_list').handler({})).artifacts;
    expect(site.open_drafts.map(o => o.draft_id)).toEqual([d.draft_id]);
    // and the bytes are the desk's, read back the way an agent would
    const back = payload(await tools.get('versioned_artifact_materialize')
      .handler({ artifact_code: 'site', version_id: saved.version_id }));
    expect(await readFile(path.join(back.path, 'index.html'), 'utf8')).toBe('<h1>v2</h1>\n');
  });

  it('refuses to save a draft whose desk is gone, and creates no version', async () => {
    // Absence is not emptiness: creating the desk here would read as "the agent
    // deleted every file" and mint an empty version over a good one.
    const tools = await liveTools();
    const d = payload(await tools.get('versioned_artifact_create_draft')
      .handler({ artifact_code: 'site', base_version: 'current', description: 'x' }));
    const before = payload(await tools.get('versioned_artifact_list_versions').handler({ artifact_code: 'site', limit: 50 }));
    await rm(d.path, { recursive: true, force: true });

    const res = payload(await tools.get('versioned_artifact_save_version')
      .handler({ artifact_code: 'site', draft_id: d.draft_id, description: 'second' }));

    expect(JSON.stringify(res)).toContain('DESK_MISSING');
    const after = payload(await tools.get('versioned_artifact_list_versions').handler({ artifact_code: 'site', limit: 50 }));
    expect(after.versions).toEqual(before.versions);
  });

  it('refuses a read that names both a draft and a version, and leaves the desk alone', async () => {
    // The tool layer decides the desk's NAME from the selector, so a selector naming
    // two sources must be refused BEFORE the desk is emptied — otherwise a malformed
    // read destroys work the agent has not saved.
    const tools = await liveTools();
    const d = payload(await tools.get('versioned_artifact_create_draft')
      .handler({ artifact_code: 'site', base_version: 'current', description: 'x' }));
    await writeFile(path.join(d.path, 'unsaved.txt'), 'work in progress');

    for (const args of [{ draft_id: d.draft_id, version_id: 'v-20250101-000000-aaaa' }, {}]) {
      const res = await tools.get('versioned_artifact_materialize')
        .handler({ artifact_code: 'site', ...args });
      expect(JSON.stringify(res)).toContain('INVALID_PATH');
    }
    expect((await readdir(d.path)).sort()).toEqual(['index.html', 'unsaved.txt']);
  });

  it('leaves the desk alone when the read it is asked for is refused', async () => {
    // The destination is cleared only once the SOURCE is proven. A discarded draft is the
    // sharpest case: the desk still holds work the agent never saved, and the store can no
    // longer answer — an empty desk plus DRAFT_NOT_FOUND would be the worst of both.
    const tools = await liveTools();
    const d = payload(await tools.get('versioned_artifact_create_draft')
      .handler({ artifact_code: 'site', base_version: 'current', description: 'x' }));
    await writeFile(path.join(d.path, 'unsaved.txt'), 'work in progress');
    const before = (await readdir(d.path)).sort();
    await tools.get('versioned_artifact_discard_draft')
      .handler({ artifact_code: 'site', draft_id: d.draft_id });

    const res = await tools.get('versioned_artifact_materialize')
      .handler({ artifact_code: 'site', draft_id: d.draft_id });

    expect(JSON.stringify(res)).toContain('DRAFT_NOT_FOUND');
    expect((await readdir(d.path)).sort()).toEqual(before);
  });

  it('refuses every desk-touching tool when the session has no workspace', async () => {
    // `_fallback` is shared by every job that has no agent_view meta, so writing a
    // desk there would mix two jobs' bytes.
    for (const artifactsDir of ['/workspace/artifacts/_fallback', undefined]) {
      const tools = await liveTools({ artifactsDir });
      const d = await tools.get('versioned_artifact_create_draft')
        .handler({ artifact_code: 'site', base_version: 'current', description: 'x' });
      expect(JSON.stringify(d)).toContain('WORKSPACE_UNAVAILABLE');
      const m = await tools.get('versioned_artifact_materialize')
        .handler({ artifact_code: 'site', version_id: 'v-20250101-000000-aaaa' });
      expect(JSON.stringify(m)).toContain('WORKSPACE_UNAVAILABLE');
      const sv = await tools.get('versioned_artifact_save_version')
        .handler({ artifact_code: 'site', draft_id: 'd-abcdef', description: 'x' });
      expect(JSON.stringify(sv)).toContain('WORKSPACE_UNAVAILABLE');
    }
  });
});

});   // describe('against a real store')
