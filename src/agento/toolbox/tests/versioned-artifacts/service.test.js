import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, mkdir, writeFile, readFile, stat, readdir } from 'node:fs/promises';
import fs from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createService } from '../../../modules/versioned_artifacts/toolbox/service.js';
import { createBackend } from '../../../modules/versioned_artifacts/toolbox/git-backend.js';
import { readSource } from './helpers.js';   // test-local, see Task 5 Step 1a

const execFileAsync = promisify(execFile);

// The desk-taking service calls route through `mirrorIn`/`mirrorOut`, which are
// fd-anchored and call `requireLinux()`. Everything else in this file is portable.
const linux = process.platform === 'linux';

let root, pub, src, svc, rows, desk, deskFd;
const opened = [];
const cfg = (over = {}) => ({ storage_root: root, published_root: pub, allowed_artifacts: 'site',
  'serving/keep_versions': 0, 'serving/public_base_url': 'http://localhost:8080', 'limits/max_files': 2000,
  'limits/max_file_size': 5242880, 'limits/max_total_size': 104857600, 'limits/max_diff_bytes': 1048576,
  'limits/max_agent_artifacts': 50,
  'security/allow_symlinks': false, ...over });

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'vf-svc-'));
  pub = await mkdtemp(path.join(tmpdir(), 'vf-svc-pub-'));
  src = await mkdtemp(path.join(tmpdir(), 'vf-src-'));
  await writeFile(path.join(src, 'index.html'), '<h1>v1</h1>\n');
  rows = [];
  const pool = { execute: async (_sql, params) => { rows.push(params); } };
  svc = createService({ config: cfg(), db: { getCronPool: () => pool }, log: vi.fn(),
    jobId: 12345, agentViewId: 7, actor: 'a@b.c' });
  await svc.init('site', { files: await readSource(src) });
  desk = await mkdtemp(path.join(tmpdir(), 'vf-desk-'));
  if (linux) {
    deskFd = fs.openSync(desk, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY);
    opened.push(deskFd);
  }
});
afterEach(async () => {
  while (opened.length) { try { fs.closeSync(opened.pop()); } catch { /* already closed */ } }
  await rm(root, {recursive:true,force:true});
  await rm(pub, {recursive:true,force:true});
  await rm(src, {recursive:true,force:true});
  await rm(desk, {recursive:true,force:true});
});

describe('configuration validation', () => {
  it('parses booleans, integers and the storage root rather than trusting their wire type', () => {
    // A DB/ENV override arrives as a STRING, so the safe values must parse as
    // false rather than as "truthy string" — otherwise turning the flag off
    // would be what breaks the module.
    for (const off of [false, 'false', '0', 'no', '']) {
      expect(() => createService({ config: cfg({ 'security/allow_symlinks': off }), db: null, log: vi.fn() })).not.toThrow();
    }
    expect(() => createService({ config: cfg({ 'security/allow_symlinks': 'maybe' }), db: null, log: vi.fn() }))
      .toThrow(/must be a boolean/);
    for (const bad of ['2ooo', '', '0', '-1', '1.5', undefined]) {
      expect(() => createService({ config: cfg({ 'limits/max_files': bad }), db: null, log: vi.fn() }))
        .toThrow(/max_files must be a positive integer/);
    }
    for (const bad of ['', undefined, 'relative/path', '/srv/../etc']) {
      expect(() => createService({ config: cfg({ storage_root: bad }), db: null, log: vi.fn() }))
        .toThrow(/storage_root/);
    }
  });

  it('refuses to construct with allow_symlinks true (unsupported in MVP)', () => {
    expect(() => createService({ config: cfg({ 'security/allow_symlinks': 'true' }), db: null, log: vi.fn() }))
      .toThrow(/allow_symlinks/);
  });
});

describe('authorization', () => {
  it('denies an artifact outside the allowlist', async () => {
    await expect(svc.getCurrent('other')).rejects.toThrow(/ARTIFACT_ACCESS_DENIED/);
  });
  it('denies every artifact when the allowlist is empty (fail closed)', async () => {
    const s = createService({ config: cfg({ allowed_artifacts: '' }), db: null, log: vi.fn() });
    await expect(s.getCurrent('site')).rejects.toThrow(/ARTIFACT_ACCESS_DENIED/);
  });
});

describe('audit', () => {
  it('records a successful mutation with the job it belongs to', async () => {
    rows.length = 0;
    const d = await svc.createDraft('site', 'current', 'x');
    const row = rows.at(-1);
    expect(row).toContain('versioned_artifact.draft.created');
    expect(row).toContain(d.draft_id);
    expect(row).toContain(12345);
    expect(row).toContain('ok');
  });

  it.skipIf(!linux)('persists the save_version description and revision, the only place a version label is kept', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    await svc.materialize('site', { draftId: d.draft_id }, deskFd);
    await writeFile(path.join(desk, 'index.html'), '<h1>v2</h1>\n');
    rows.length = 0;
    const r = await svc.saveVersion('site', d.draft_id, deskFd, 'Homepage with MCP section');
    const row = rows.at(-1);
    expect(row).toContain('versioned_artifact.version.saved');
    expect(row).toContain('Homepage with MCP section');
    expect(row).toContain(r.revision);
  });

  it('records a FAILED mutation too', async () => {
    rows.length = 0;
    // A well-formed draft id that names nothing: the refusal is what must be audited.
    await expect(svc.discardDraft('site', 'd-nosuchdraft01')).rejects.toThrow(/DRAFT_NOT_FOUND/);
    expect(rows.at(-1)).toContain('error');
  });

  it('records the artifact initialization', async () => {
    const s = createService({ config: cfg({ allowed_artifacts: 'other' }), db: { getCronPool: () => ({ execute: async (_s, p) => rows.push(p) }) },
      log: vi.fn(), actor: 'admin' });
    rows.length = 0;
    await s.init('other', { files: await readSource(src) });
    expect(rows.at(-1)).toContain('versioned_artifact.artifact.initialized');
  });

  it('persists the create_draft description too, not only the save_version one', async () => {
    // The plan claims BOTH PRD-supplied descriptions are kept (§22.2 and §22.7).
    // Only the version label was asserted, so half the claim was untested.
    rows.length = 0;
    await svc.createDraft('site', 'current', 'Seasonal banner refresh');
    const row = rows.at(-1);
    expect(row).toContain('versioned_artifact.draft.created');
    expect(row).toContain('Seasonal banner refresh');
  });

  it('falls back to a file when the DB insert fails, rather than losing the event', async () => {
    const s = createService({ config: cfg(), db: { getCronPool: () => ({ execute: async () => { throw new Error('db down'); } }) },
      log: vi.fn(), actor: 'a@b.c' });
    await s.createDraft('site', 'current', 'x');
    const line = await readFile(path.join(root, 'audit-fallback.log'), 'utf8');
    expect(line).toContain('versioned_artifact.draft.created');
  });

  it('bounds BOTH caller-supplied fields in the FALLBACK file, not only in the SQL parameter', async () => {
    // The truncation used to live in the parameter list, so the DB column was
    // bounded and the file — the sink an agent reaches when the DB is down —
    // was not. A limit that only applies on the healthy path is not a limit.
    // `actor` is asserted beside `description` because it was the field left
    // behind when `description` was bounded: one `cap()` now covers both, and
    // this is the test that keeps them together.
    const s = createService({ config: cfg(), db: { getCronPool: () => ({ execute: async () => { throw new Error('db down'); } }) },
      log: vi.fn(), actor: `${'a'.repeat(5000)}@b.c` });
    await s.createDraft('site', 'current', 'D'.repeat(5000));
    const rec = JSON.parse((await readFile(path.join(root, 'audit-fallback.log'), 'utf8')).trim());
    expect(rec.description).toHaveLength(255);
    expect(rec.actor).toHaveLength(255);
  });

  it('does not fail createDraft when reclaiming an unrelated orphan fails', async () => {
    // Reconciliation is opportunistic cleanup of garbage this call did not
    // create. Its failure is logged by the service — the layer that owns a
    // logger — and the valid creation proceeds.
    const log = vi.fn();
    const s = createService({ config: cfg(), db: { getCronPool: () => ({ execute: async (_s, p) => rows.push(p) }) },
      log, actor: 'a@b.c',
      backend: { ...createBackend(), reconcileDrafts: async () => { throw new Error('unreadable'); } } });
    await expect(s.createDraft('site', 'current', 'x')).resolves.toHaveProperty('draft_id');
    expect(log).toHaveBeenCalledWith('versioned_artifacts', 'ERROR', expect.stringMatching(/reconcil/i));
  });
});

describe('resuming a partial teardown through the PUBLIC path', () => {
  // `discard_draft` is the only operation that still tears a worktree down — a save
  // keeps the draft open — so it is the only one whose resume can be exercised. The
  // service's ordering rule is what these prove: draftState() BEFORE recoverDraft(),
  // because recovery runs inside a worktree that teardown has already removed.
  const removeWorktree = async (draftId) =>
    rm(path.join(root, 'site', 'worktrees', draftId), { recursive: true, force: true });

  it('resumes a discard whose worktree removal already happened', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    await execFileAsync('git', ['--git-dir', path.join(root, 'site/repo.git'),
      'update-ref', `refs/agento/discarding/${d.draft_id}`, 'refs/agento/current']);
    await removeWorktree(d.draft_id);
    await expect(svc.discardDraft('site', d.draft_id)).resolves.toEqual({ draft_id: d.draft_id, discarded: true });
  });

  it('reports DRAFT_NOT_FOUND for a mutation on a DISCARDED draft', async () => {
    // Saving no longer closes a draft, so the closed state a mutation must be
    // refused on is the discarded one.
    const d = await svc.createDraft('site', 'current', 'x');
    await svc.discardDraft('site', d.draft_id);
    await expect(svc.discardDraft('site', d.draft_id)).rejects.toThrow(/DRAFT_NOT_FOUND/);
  });
});

describe('concurrency', () => {
  it.skipIf(!linux)('serializes concurrent save_version on the SAME draft', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    await svc.materialize('site', { draftId: d.draft_id }, deskFd);
    await writeFile(path.join(desk, 'p.txt'), '1');
    const results = await Promise.allSettled([
      svc.saveVersion('site', d.draft_id, deskFd, 'one'),
      svc.saveVersion('site', d.draft_id, deskFd, 'two'),
    ]);
    // Either both saved in some order, or the loser reported DRAFT_LOCKED. What must
    // NOT happen is a torn mirror — two mirrors interleaved into one commit.
    const saved = results.filter(r => r.status === 'fulfilled').map(r => r.value.version_id);
    for (const id of saved) {
      expect((await svc.listVersions('site')).map(v => v.version_id)).toContain(id);
    }
    for (const r of results.filter(r => r.status === 'rejected')) expect(String(r.reason)).toMatch(/DRAFT_LOCKED/);
  });

  it.skipIf(!linux)('holds the lock a save takes while it replaces that draft\'s desk', async () => {
    // `materialize` empties the desk a save reads from, so the two must serialize on ONE
    // lock. Asserted by the acquire primitive itself: `withLock` takes the lock with
    // `mkdir`, so a `mkdir` of the lifecycle lock path must fail with EEXIST mid-copy.
    const d = await svc.createDraft('site', 'current', 'x');
    const saveLock = path.join(root, '.locks', 'site', 'lifecycle.lock');
    const real = createBackend();
    let held = null;
    const watched = { ...real, materialize: async (...args) => {
      held = await mkdir(saveLock).then(() => 'free', (e) => e.code);
      return real.materialize(...args);
    } };
    const s = createService({ config: cfg(), db: null, log: vi.fn(), backend: watched });

    await s.materialize('site', { draftId: d.draft_id }, deskFd);

    expect(held).toBe('EEXIST');
    // And it is released, so the next save is not locked out.
    expect((await svc.saveVersion('site', d.draft_id, deskFd, 'after')).version_id).toMatch(/^v-/);
  });

  it.skipIf(!linux)('does not run recovery on a read path', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    const wt = await svc.internalDraftPath('site', d.draft_id);
    await writeFile(path.join(wt, 'in-flight.txt'), 'a concurrent write in progress');
    await svc.materialize('site', { draftId: d.draft_id }, deskFd);
    // Recovery on a read would delete another operation's in-progress work — and the
    // read itself answers from the draft tip, so the desk never sees the torn file.
    await expect(stat(path.join(wt, 'in-flight.txt'))).resolves.toBeTruthy();
    expect(await readdir(desk)).toEqual(['index.html']);
  });

  it('recovers a dirty draft before a MUTATION', async () => {
    // The only mutation left that can observe recovery is the save, and its own
    // mirror overwrites the worktree — so the assertion is on the ORDER, which is
    // what `prepareDraft` exists to guarantee for every future mutation too.
    const calls = [];
    const real = createBackend();
    const s = createService({ config: cfg(), db: null, log: vi.fn(), actor: 'a@b.c',
      backend: { ...real,
        recoverDraft: async (...a) => { calls.push('recover'); return real.recoverDraft(...a); },
        saveVersion: async (_r, _c, draftId) => { calls.push('save'); return { version_id: 'v-20250101-000000-aaaa', source_draft: draftId, revision: 'abc1234' }; } } });
    const d = await s.createDraft('site', 'current', 'x');
    calls.length = 0;
    await s.saveVersion('site', d.draft_id, -1, 'm');
    expect(calls).toEqual(['recover', 'save']);
  });
});

describe('listing the artifacts a scope may use', () => {
  // `meta` is the `versioned_artifact` table; phase 4 writes its rows, so every
  // artifact must list correctly with no row at all.
  const withMeta = (meta, over = {}) => createService({
    config: cfg(over), log: vi.fn(), actor: 'a@b.c',
    db: { getCronPool: () => ({ query: async () => [meta], execute: async () => {} }) },
  });

  it('lists the allowed artifacts that the store actually holds, and nothing else', async () => {
    // In the store, not in THIS scope's allowlist — so it takes a service the
    // operator granted it, the way the admin CLI would have created it.
    await createService({ config: cfg({ allowed_artifacts: 'other' }), db: null, log: vi.fn() })
      .init('other', { files: await readSource(src) });
    const s = withMeta([], { allowed_artifacts: 'site,never-created' });
    expect((await s.listArtifacts()).map(a => a.artifact_code)).toEqual(['site']);
  });

  it('reports the current version and the open drafts of each', async () => {
    // Without the drafts the recovery story does not close: a retried job starts
    // with a wiped desk and no memory of the draft_id it left behind.
    const d = await svc.createDraft('site', 'current', 'x');
    const gone = await svc.createDraft('site', 'current', 'y');
    await svc.discardDraft('site', gone.draft_id);
    const [site] = await withMeta([]).listArtifacts();
    expect(site.current_version).toBe((await svc.getCurrent('site')).current_version);
    expect(site.open_drafts).toEqual([{ draft_id: d.draft_id, base_version: site.current_version }]);
  });

  it('takes title, owner and created_at from the metadata row when one exists', async () => {
    const created = new Date('2026-09-11T08:30:00Z');
    const [site] = await withMeta([
      { artifact_code: 'site', title: 'Marketing site', owner: 'a@b.c', created_at: created },
    ]).listArtifacts();
    expect(site).toMatchObject({ title: 'Marketing site', owner: 'a@b.c', created_at: created });
  });

  it('lists an artifact that has no metadata row yet', async () => {
    const [site] = await withMeta([{ artifact_code: 'unrelated', title: 't', owner: 'o' }]).listArtifacts();
    expect(site).toMatchObject({ artifact_code: 'site', title: null, owner: null, created_at: null });
  });

  it('still lists the store when the metadata table cannot be read', async () => {
    // The store is the authority on what exists; the table only decorates it. A DB
    // outage must not hide the artifact an agent is mid-job on.
    const s = createService({ config: cfg(), log: vi.fn(), actor: 'a@b.c',
      db: { getCronPool: () => ({ query: async () => { throw new Error('db down'); } }) } });
    expect((await s.listArtifacts()).map(a => a.artifact_code)).toEqual(['site']);
    const noDb = createService({ config: cfg(), db: null, log: vi.fn(), actor: 'a@b.c' });
    expect((await noDb.listArtifacts()).map(a => a.artifact_code)).toEqual(['site']);
  });
});

it('never returns a host path or a raw git error', async () => {
  const s = createService({ config: cfg({ storage_root: '/nonexistent/vf-other-root' }), db: null, log: vi.fn() });
  const err = await s.getCurrent('site').catch(e => e);
  expect(String(err.detail ?? err.message)).not.toMatch(/vf-other-root|fatal:|node:internal/);
});

describe('the artifacts an agent_view may create', () => {
  // `agent_view_id` is SELF-ASSERTED by the caller, so ownership SCOPES artifacts
  // between cooperating agent_views — it is not an authorization boundary. The tests
  // below pin the scoping rules; ROADMAP.md carries the identity gap.
  //
  // Ownership used to live in an `av<id>-` code PREFIX. It now lives in the artifact,
  // recorded by the store at creation, so a code is a plain name: the agent neither
  // types the prefix nor wears it in the published URL.
  const view = (id, over = {}) => createService({
    config: cfg({ allowed_artifacts: '', ...over }), db: null, log: vi.fn(), agentViewId: id,
  });

  it('creates under a plain name with no operator grant at all', async () => {
    const s = view(7);
    expect((await s.init('report')).artifact_code).toBe('report');
  });

  it('lists what it created and nothing another view owns', async () => {
    await view(7).init('report');
    await view(9).init('other');
    expect((await view(9).listArtifacts()).map(a => a.artifact_code)).toEqual(['other']);
  });

  it('answers a taken name with the next free number instead of an error', async () => {
    // The caller cannot see what another agent_view already took — the store is shared
    // and the listing is not — so a refusal would be advice it has no way to act on.
    await view(7).init('report');
    expect((await view(9).init('report')).artifact_code).toBe('report-2');
    expect((await view(9).init('report')).artifact_code).toBe('report-3');
  });

  it('appends rather than splicing over a number the name already ends with', async () => {
    // Stripping a trailing `-N` first would turn a taken `plan-2024` into `plan-2` and
    // silently discard the year. Ugly and truthful beats tidy and wrong.
    await view(7).init('plan-2024');
    expect((await view(9).init('plan-2024')).artifact_code).toBe('plan-2024-2');
  });

  it('names the artifact that EXISTS in its answer, including the preview url', async () => {
    await view(7).init('report');
    const r = await view(9).init('report');
    expect(r.artifact_code).toBe('report-2');
    expect(r.preview_url).toContain('/report-2/');
  });

  it('does not reach an artifact another view created under a name it also asked for', async () => {
    await view(7).init('report');
    await expect(view(9).getCurrent('report')).rejects.toMatchObject({ code: 'ARTIFACT_ACCESS_DENIED' });
  });

  it('owns nothing when the agent_view id is not a positive integer', async () => {
    // `?agent_view_id=abc` parses to NaN and `?agent_view_id=0` to 0. Neither may own
    // an artifact, so neither may create one without an operator grant.
    for (const id of [Number.NaN, null, 0, -1, 1.5]) {
      const s = view(id);
      await expect(s.init('anything')).rejects.toThrow(/ARTIFACT_ACCESS_DENIED/);
      expect(await s.listArtifacts()).toEqual([]);
    }
  });

  it('records an audit row for a refused creation', async () => {
    const audited = [];
    const s = createService({ config: cfg({ allowed_artifacts: '' }), agentViewId: 0, log: vi.fn(),
      db: { getCronPool: () => ({ execute: async (_q, p) => audited.push(p) }) } });
    await expect(s.init('report')).rejects.toThrow(/ARTIFACT_ACCESS_DENIED/);
    expect(audited.at(-1)).toContain('ARTIFACT_ACCESS_DENIED');
  });

  it('creates an artifact an operator pre-granted', async () => {
    // The answer to "I want a specific URL": one `config:set allowed_artifacts`, and the
    // agent does the rest. The same grant is how one agent_view hands an artifact to another.
    const s = view(7, { allowed_artifacts: 'marketing-site' });
    expect((await s.init('marketing-site')).artifact_code).toBe('marketing-site');
  });

  it('never renames a name the OPERATOR chose', async () => {
    // The whole point of a grant is that address. Quietly publishing at `-2` instead
    // would answer a request nobody made.
    const s = view(7, { allowed_artifacts: 'marketing-site' });
    await s.init('marketing-site');
    await expect(view(9, { allowed_artifacts: 'marketing-site' }).init('marketing-site'))
      .rejects.toMatchObject({ code: 'ARTIFACT_ALREADY_EXISTS' });
  });

  it('stops at the cap, and counts only what the caller may use', async () => {
    // A cap counted over the WHOLE store would be a cross-view cardinality oracle and,
    // with no delete anywhere, a one-way lockout of every other agent_view.
    // A view of its own: the suite's `beforeEach` already created `site` as view 7,
    // which now OWNS it and so would start one artifact into the cap.
    const eleven = view(11, { 'limits/max_agent_artifacts': 2 });
    await eleven.init('a');
    await eleven.init('b');
    const err = await eleven.init('c').catch(e => e);
    expect(err.code).toBe('ARTIFACT_LIMIT_REACHED');
    expect((await view(9, { 'limits/max_agent_artifacts': 2 }).init('d')).artifact_code).toBe('d');
  });

  it('serializes one owner\'s concurrent creations so the cap cannot be raced', async () => {
    // Two inits of DIFFERENT names by the same owner: without the owner lock both read
    // the same pre-create count and both slip past a cap of one. Exactly one must win.
    const v = view(13, { 'limits/max_agent_artifacts': 1 });
    const results = await Promise.allSettled([v.init('a'), v.init('b')]);
    expect(results.filter(r => r.status === 'fulfilled')).toHaveLength(1);
    const capped = results.filter(r => r.status === 'rejected');
    expect(capped).toHaveLength(1);
    expect(String(capped[0].reason)).toMatch(/ARTIFACT_LIMIT_REACHED/);
  });

  it('exempts the administrator from the namespace and the cap', async () => {
    // `admin: true` is set by cli.js alone: `artifact:init` must keep creating any code.
    const admin = createService({ config: cfg({ allowed_artifacts: '', 'limits/max_agent_artifacts': 1 }),
      db: null, log: vi.fn(), admin: true });
    await admin.init('operator-one');
    expect((await admin.init('operator-two')).artifact_code).toBe('operator-two');
  });
});

describe('remove', () => {
  // Admin-only, and refused INSIDE the audit boundary so the attempt leaves a row.
  it('refuses a non-admin caller', async () => {
    await expect(svc.remove('site')).rejects.toMatchObject({ code: 'ARTIFACT_ACCESS_DENIED' });
  });

  it('removes the published tree, the store and the row', async () => {
    const deleted = [];
    const pool = { execute: async (sql, params) => { deleted.push([sql, params]); } };
    const admin = createService({ config: cfg(), db: { getCronPool: () => pool }, log: vi.fn(),
      actor: 'admin', admin: true });
    const r = await admin.remove('site');
    expect(r).toMatchObject({ artifact_code: 'site', removed_store: true, removed_published: true });
    await expect(stat(path.join(root, 'site'))).rejects.toMatchObject({ code: 'ENOENT' });
    await expect(stat(path.join(pub, 'site'))).rejects.toMatchObject({ code: 'ENOENT' });
    expect(deleted.some(([sql, params]) => /DELETE FROM versioned_artifact\b/.test(sql) && params[0] === 'site'))
      .toBe(true);
  });

  // The half-cleaned state is exactly what an operator reaches for this command to
  // repair: the store gone, the pages still being served.
  it('repairs a half-removed artifact', async () => {
    await rm(path.join(root, 'site'), { recursive: true, force: true });
    const admin = createService({ config: cfg(), db: null, log: vi.fn(), actor: 'admin', admin: true });
    const r = await admin.remove('site');
    expect(r).toMatchObject({ removed_store: false, removed_published: true });
    await expect(stat(path.join(pub, 'site'))).rejects.toMatchObject({ code: 'ENOENT' });
  });

  it('reports not found when neither root holds it', async () => {
    const admin = createService({ config: cfg(), db: null, log: vi.fn(), actor: 'admin', admin: true });
    await expect(admin.remove('absent')).rejects.toMatchObject({ code: 'ARTIFACT_NOT_FOUND' });
  });

  it('rejects an invalid code above the audit boundary', async () => {
    const admin = createService({ config: cfg(), db: null, log: vi.fn(), actor: 'admin', admin: true });
    await expect(admin.remove('../escape')).rejects.toMatchObject({ code: 'INVALID_PATH' });
  });
});

describe('delete is excluded by an active mutation on the same artifact', () => {
  // remove and every mutation take the ONE lifecycle lock, which lives outside the
  // removed root, so a delete never runs beside an in-flight draft, save or publish.
  // Each case pauses the mutation inside the backend and asserts remove cannot proceed
  // until the mutation releases the lock.
  const runsAfter = async (method, drive) => {
    let open; const gate = new Promise((r) => { open = r; });
    const real = createBackend();
    const order = [];
    const watched = { ...real, [method]: async (...a) => {
      order.push('mut'); await gate; return real[method](...a);
    } };
    const actor = createService({ config: cfg(), db: null, log: vi.fn(), agentViewId: 7, backend: watched });
    const admin = createService({ config: cfg(), db: null, log: vi.fn(), actor: 'admin', admin: true, backend: real });
    const mutating = drive(actor);
    await new Promise((r) => setTimeout(r, 40));           // the mutation enters and holds the lock
    const removing = admin.remove('site').then(() => order.push('remove'));
    await new Promise((r) => setTimeout(r, 40));           // remove must be BLOCKED on the lock
    expect(order).toEqual(['mut']);
    open();
    await Promise.allSettled([mutating, removing]);
    expect(order).toEqual(['mut', 'remove']);              // remove ran only after release
  };

  it('waits for an in-flight create_draft', () => runsAfter('createDraft',
    (s) => s.createDraft('site', 'current', 'x')));

  it.skipIf(!linux)('waits for an in-flight save_version', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    await svc.materialize('site', { draftId: d.draft_id }, deskFd);
    await writeFile(path.join(desk, 'p.txt'), '1');
    await runsAfter('saveVersion', (s) => s.saveVersion('site', d.draft_id, deskFd, 'one'));
  });

  it.skipIf(!linux)('waits for an in-flight publish', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    await svc.materialize('site', { draftId: d.draft_id }, deskFd);
    await writeFile(path.join(desk, 'p.txt'), '1');
    const { version_id } = await svc.saveVersion('site', d.draft_id, deskFd, 'v2');
    const { current_version } = await svc.getCurrent('site');
    await runsAfter('publish', (s) => s.publish('site', version_id, current_version));
  });
});
