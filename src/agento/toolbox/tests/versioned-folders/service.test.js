import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, writeFile, readFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createService } from '../../../modules/versioned_folders/toolbox/service.js';
import { createBackend } from '../../../modules/versioned_folders/toolbox/git-backend.js';
import { readSource } from './helpers.js';   // test-local, see Task 5 Step 1a

const execFileAsync = promisify(execFile);

let root, src, svc, rows;
const cfg = (over = {}) => ({ storage_root: root, allowed_folders: 'site', 'limits/max_files': 2000,
  'limits/max_file_size': 5242880, 'limits/max_total_size': 104857600, 'limits/max_diff_bytes': 1048576,
  'security/allow_symlinks': false, ...over });

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'vf-svc-'));
  src = await mkdtemp(path.join(tmpdir(), 'vf-src-'));
  await writeFile(path.join(src, 'index.html'), '<h1>v1</h1>\n');
  rows = [];
  const pool = { execute: async (_sql, params) => { rows.push(params); } };
  svc = createService({ config: cfg(), db: { getCronPool: () => pool }, log: vi.fn(),
    jobId: 12345, agentViewId: 7, actor: 'a@b.c' });
  await svc.init('site', { files: await readSource(src) });
});
afterEach(async () => { await rm(root, {recursive:true,force:true}); await rm(src, {recursive:true,force:true}); });

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
  it('denies a folder outside the allowlist', async () => {
    await expect(svc.getCurrent('other')).rejects.toThrow(/FOLDER_ACCESS_DENIED/);
  });
  it('denies every folder when the allowlist is empty (fail closed)', async () => {
    const s = createService({ config: cfg({ allowed_folders: '' }), db: null, log: vi.fn() });
    await expect(s.getCurrent('site')).rejects.toThrow(/FOLDER_ACCESS_DENIED/);
  });
});

describe('audit', () => {
  it('records a successful mutation with the revision', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    rows.length = 0;
    const r = await svc.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'a' }], [], 'm');
    const row = rows.at(-1);
    expect(row).toContain('versioned_folder.draft.changed');
    expect(row).toContain(r.revision);
    expect(row).toContain(12345);
    expect(row).toContain('ok');
  });

  it('persists the finalize description, the only place a version label is kept', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    await svc.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'a' }], [], 'm');
    rows.length = 0;
    await svc.finalize('site', d.draft_id, 'Homepage with MCP section');
    const row = rows.at(-1);
    expect(row).toContain('versioned_folder.version.finalized');
    expect(row).toContain('Homepage with MCP section');
  });

  it('records a FAILED mutation too', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    rows.length = 0;
    await expect(svc.applyChanges('site', d.draft_id, [{ path: '../x', content: 'a' }], [], 'm')).rejects.toThrow();
    expect(rows.at(-1)).toContain('error');
  });

  it('records the folder initialization', async () => {
    const s = createService({ config: cfg({ allowed_folders: 'other' }), db: { getCronPool: () => ({ execute: async (_s, p) => rows.push(p) }) },
      log: vi.fn(), actor: 'admin' });
    rows.length = 0;
    await s.init('other', { files: await readSource(src) });
    expect(rows.at(-1)).toContain('versioned_folder.folder.initialized');
  });

  it('persists the create_draft description too, not only the finalize one', async () => {
    // The plan claims BOTH PRD-supplied descriptions are kept (§22.2 and §22.7).
    // Only finalize was asserted, so half the claim was untested.
    rows.length = 0;
    await svc.createDraft('site', 'current', 'Seasonal banner refresh');
    const row = rows.at(-1);
    expect(row).toContain('versioned_folder.draft.created');
    expect(row).toContain('Seasonal banner refresh');
  });

  it('falls back to a file when the DB insert fails, rather than losing the event', async () => {
    const s = createService({ config: cfg(), db: { getCronPool: () => ({ execute: async () => { throw new Error('db down'); } }) },
      log: vi.fn(), actor: 'a@b.c' });
    const d = await s.createDraft('site', 'current', 'x');
    await s.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'a' }], [], 'm');
    const line = await readFile(path.join(root, 'audit-fallback.log'), 'utf8');
    expect(line).toContain('versioned_folder.draft.changed');
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

  it('audits the ACTOR the call was made for, not the one the service was built with', async () => {
    // forActor is what makes a service constructed once per register() still
    // record per-request identity (Task 8). Without it every MCP mutation row
    // carries the registration-time actor, which is nobody.
    rows.length = 0;
    await svc.forActor('caller@b.c').createDraft('site', 'current', 'x');
    expect(rows.at(-1)).toContain('caller@b.c');
    expect(rows.at(-1)).not.toContain('a@b.c');
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
    expect(log).toHaveBeenCalledWith('versioned_folders', 'ERROR', expect.stringMatching(/reconcil/i));
  });
});

describe('resuming a partial teardown through the PUBLIC path', () => {
  // The backend's own retry tests call finalize()/discardDraft() directly and so
  // never exercise the service's ordering rule. These do: the service must ask
  // draftState() BEFORE recoverDraft(), because recovery runs inside a worktree
  // that teardown has already removed.
  const removeWorktree = async (draftId) =>
    rm(path.join(root, 'site', 'worktrees', draftId), { recursive: true, force: true });

  it('resumes a finalize whose worktree removal already happened', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    await svc.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'one' }], [], 'm');
    const { version_id } = await svc.finalize('site', d.draft_id, 'v1');
    // Simulate the crash shape: version + marker written, teardown incomplete.
    await execFileAsync('git', ['--git-dir', path.join(root, 'site/repo.git'),
      'update-ref', `refs/agento/finalized/${d.draft_id}`,
      (await svc.listVersions('site')).find(v => v.version_id === version_id).revision]);
    await removeWorktree(d.draft_id);
    // A retry must converge on the SAME version, not fail inside recovery.
    await expect(svc.finalize('site', d.draft_id, 'v1')).resolves.toMatchObject({ version_id });
  });

  it('resumes a discard whose worktree removal already happened', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    await execFileAsync('git', ['--git-dir', path.join(root, 'site/repo.git'),
      'update-ref', `refs/agento/discarding/${d.draft_id}`, 'refs/agento/current']);
    await removeWorktree(d.draft_id);
    await expect(svc.discardDraft('site', d.draft_id)).resolves.toEqual({ draft_id: d.draft_id, discarded: true });
  });

  it('reports DRAFT_NOT_FOUND for a mutation on a finalized draft', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    await svc.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'one' }], [], 'm');
    await svc.finalize('site', d.draft_id, 'v1');
    await expect(svc.applyChanges('site', d.draft_id, [{ path: 'b.txt', content: 'x' }], [], 'm2'))
      .rejects.toThrow(/DRAFT_NOT_FOUND/);
  });
});

describe('concurrency', () => {
  it('serializes concurrent apply_changes on the SAME draft', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    const results = await Promise.allSettled([
      svc.applyChanges('site', d.draft_id, [{ path: 'p.txt', content: '1' }], [], 'one'),
      svc.applyChanges('site', d.draft_id, [{ path: 'q.txt', content: '2' }], [], 'two'),
    ]);
    // Either both committed in some order, or the loser reported DRAFT_LOCKED.
    // What must NOT happen is a lost or interleaved write.
    const files = (await svc.listFiles('site', { draftId: d.draft_id })).map(f => f.path);
    for (const r of results.filter(r => r.status === 'fulfilled')) expect(files).toContain(r.value.changed[0]);
    for (const r of results.filter(r => r.status === 'rejected')) expect(String(r.reason)).toMatch(/DRAFT_LOCKED/);
  });

  it('a concurrent read never observes a half-written file', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    await svc.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'committed' }], [], 'm');
    // Reads are tree-based, so an in-flight mutation cannot corrupt them AND a
    // read can never trigger recovery that deletes a concurrent write.
    const [, readBack] = await Promise.all([
      svc.applyChanges('site', d.draft_id, [{ path: 'b.txt', content: 'second' }], [], 'm2'),
      svc.readFile('site', { draftId: d.draft_id }, 'a.txt'),
    ]);
    expect(readBack.content).toBe('committed');
  });

  it('does not run recovery on a read path', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    await svc.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'committed' }], [], 'm');
    const wt = svc.internalDraftPath('site', d.draft_id);
    await writeFile(path.join(wt, 'in-flight.txt'), 'a concurrent write in progress');
    await svc.listFiles('site', { draftId: d.draft_id });
    // Recovery on a read would delete another operation's in-progress work.
    await expect(stat(path.join(wt, 'in-flight.txt'))).resolves.toBeTruthy();
  });

  it('recovers a dirty draft before a MUTATION', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    await svc.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'committed' }], [], 'm');
    await writeFile(path.join(svc.internalDraftPath('site', d.draft_id), 'a.txt'), 'HALF');
    await svc.applyChanges('site', d.draft_id, [{ path: 'c.txt', content: 'c' }], [], 'm2');
    expect((await svc.readFile('site', { draftId: d.draft_id }, 'a.txt')).content).toBe('committed');
  });
});

it('never returns a host path or a raw git error', async () => {
  const s = createService({ config: cfg({ storage_root: '/nonexistent/vf-other-root' }), db: null, log: vi.fn() });
  const err = await s.getCurrent('site').catch(e => e);
  expect(String(err.detail ?? err.message)).not.toMatch(/vf-other-root|fatal:|node:internal/);
});
