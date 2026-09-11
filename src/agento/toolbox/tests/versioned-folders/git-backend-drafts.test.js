import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, writeFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createBackend } from '../../../modules/versioned_folders/toolbox/git-backend.js';
import { readSource, lastMessage, draftDirs } from './helpers.js';   // test-local, see Step 1a

let root, src, v1, be;
beforeEach(async () => {
  be = createBackend();
  root = await mkdtemp(path.join(tmpdir(), 'vf-'));
  src = await mkdtemp(path.join(tmpdir(), 'vf-src-'));
  await writeFile(path.join(src, 'index.html'), '<h1>v1</h1>\n');
  ({ current_version: v1 } = await be.init(root, 'site', { files: await readSource(src) }));
});
afterEach(async () => { await rm(root, {recursive:true,force:true}); await rm(src, {recursive:true,force:true}); });

describe('draft lifecycle', () => {
  it('creates a draft from current and records the base version', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'redesign');
    expect(d.draft_id).toMatch(/^d-[a-z0-9]{6,32}$/);
    expect(d.base_version).toBe(v1);
    expect((await stat(be.getDraftPath(root, 'site', d.draft_id))).isDirectory()).toBe(true);
  });

  it('keeps two concurrent drafts isolated', async () => {
    const a = await be.createDraft(root, 'site', 'current', 'a');
    const b = await be.createDraft(root, 'site', 'current', 'b');
    await be.applyChanges(root, 'site', a.draft_id, [{ path: 'a.txt', content: 'A' }], [], 'a', {});
    expect((await be.listFiles(root, 'site', { draftId: b.draft_id })).some(f => f.path === 'a.txt')).toBe(false);
  });

  it('leaves no orphan worktree when draft setup fails partway', async () => {
    await be.createDraft(root, 'site', 'current', 'x');
    const before = await draftDirs(root, 'site');
    // Force the base-ref write to fail after the worktree exists.
    const failing = createBackend({ hooks: { afterWorktree: () => { throw new Error('injected'); } } });
    await expect(failing.createDraft(root, 'site', 'current', 'y')).rejects.toThrow();
    // A leaked worktree or branch would make the folder accumulate junk and could
    // collide with a later draft id.
    expect(await draftDirs(root, 'site')).toEqual(before);
  });

  it('rejects unknown draft and unknown base version', async () => {
    await expect(be.listFiles(root, 'site', { draftId: 'd-deadbeef' })).rejects.toThrow(/DRAFT_NOT_FOUND/);
    await expect(be.createDraft(root, 'site', 'v-20200101-000000-dead', 'x')).rejects.toThrow(/VERSION_NOT_FOUND/);
  });
});

describe('applyChanges', () => {
  it('writes, deletes, and auto-commits as one revision', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    const r = await be.applyChanges(root, 'site', d.draft_id,
      [{ path: 'index.html', content: '<h1>v2</h1>\n' }, { path: 'css/style.css', content: 'body{}' }], [], 'update hero', {});
    expect(r.changed.sort()).toEqual(['css/style.css', 'index.html']);
    expect(r.revision).toMatch(/^[0-9a-f]{7,40}$/);
    expect((await be.readFile(root, 'site', { draftId: d.draft_id }, 'index.html')).content).toBe('<h1>v2</h1>\n');
  });

  it('records the required commit trailers', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await be.applyChanges(root, 'site', d.draft_id, [{ path: 'a.txt', content: 'a' }], [], 'msg',
      { trailers: { jobId: 12345, agentView: 'website-admin' } });
    // Read the commit through the public surface, not a test-only backend export.
    const { version_id } = await be.finalize(root, 'site', d.draft_id, 'v');
    // The trailers are asserted on the version's own commit message. A version
    // carries no `description` field (see Task 5), so there is nothing else here
    // that could accidentally stand in for the commit body.
    const full = await lastMessage(root, 'site', version_id);
    expect(full).toContain('Agento-Job: 12345');
    expect(full).toContain('Agento-Agent-View: website-admin');
    expect(full).toContain(`Agento-Draft: ${d.draft_id}`);
  });

  it('is all-or-nothing: one bad path leaves the draft untouched', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    const before = await be.readFile(root, 'site', { draftId: d.draft_id }, 'index.html');
    await expect(be.applyChanges(root, 'site', d.draft_id,
      [{ path: 'ok.txt', content: 'ok' }, { path: '../escape.txt', content: 'bad' }], [], 'm', {}))
      .rejects.toThrow(/INVALID_PATH/);
    expect((await be.readFile(root, 'site', { draftId: d.draft_id }, 'index.html')).content).toBe(before.content);
    expect((await be.listFiles(root, 'site', { draftId: d.draft_id })).some(f => f.path === 'ok.txt')).toBe(false);
  });

  it('rejects a write to .git', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await expect(be.applyChanges(root, 'site', d.draft_id, [{ path: '.git/config', content: 'x' }], [], 'm', {}))
      .rejects.toThrow(/INVALID_PATH/);
  });

  it('enforces each limit with its own error code', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await expect(be.applyChanges(root, 'site', d.draft_id, [{ path: 'big.txt', content: 'x'.repeat(200) }], [], 'm',
      { limits: { max_file_size: 100 } })).rejects.toThrow(/FILE_TOO_LARGE/);
    await expect(be.applyChanges(root, 'site', d.draft_id, [{ path: 'b.txt', content: 'x'.repeat(50) }], [], 'm',
      { limits: { max_total_size: 10 } })).rejects.toThrow(/FOLDER_TOO_LARGE/);
    await expect(be.applyChanges(root, 'site', d.draft_id, [{ path: 'c.txt', content: 'c' }], [], 'm',
      { limits: { max_files: 1 } })).rejects.toThrow(/TOO_MANY_FILES/);
  });

  it('measures size in BYTES, not UTF-16 code units', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    // 40 emoji = 40 JS "length" but 160 UTF-8 bytes. A length check would pass.
    await expect(be.applyChanges(root, 'site', d.draft_id, [{ path: 'e.txt', content: '🙂'.repeat(40) }], [], 'm',
      { limits: { max_file_size: 100 } })).rejects.toThrow(/FILE_TOO_LARGE/);
  });

  it('commits a file that a draft .gitignore would exclude', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await be.applyChanges(root, 'site', d.draft_id, [{ path: '.gitignore', content: 'ignored.txt\n' }], [], 'ignore', {});
    const r = await be.applyChanges(root, 'site', d.draft_id, [{ path: 'ignored.txt', content: 'still here' }], [], 'add', {});
    expect(r.changed).toContain('ignored.txt');
    const { version_id } = await be.finalize(root, 'site', d.draft_id, 'v');
    expect((await be.listFiles(root, 'site', { versionId: version_id })).map(f => f.path)).toContain('ignored.txt');
  });

  it('handles a filename containing a tab and a newline', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    const weird = 'we\tird\nname.txt';
    const r = await be.applyChanges(root, 'site', d.draft_id, [{ path: weird, content: 'ok' }], [], 'm', {});
    expect(r.changed).toEqual([weird]);
    expect((await be.listFiles(root, 'site', { draftId: d.draft_id })).map(f => f.path)).toContain(weird);
    expect((await be.readFile(root, 'site', { draftId: d.draft_id }, weird)).content).toBe('ok');
  });

  it('rejects an empty batch and a no-op rewrite with DRAFT_HAS_NO_CHANGES', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await expect(be.applyChanges(root, 'site', d.draft_id, [], [], 'nothing', {})).rejects.toThrow(/DRAFT_HAS_NO_CHANGES/);
    await expect(be.applyChanges(root, 'site', d.draft_id, [{ path: 'index.html', content: '<h1>v1</h1>\n' }], [], 'same', {}))
      .rejects.toThrow(/DRAFT_HAS_NO_CHANGES/);
  });

  it('reports FILE_NOT_FOUND when deleting a missing file', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await expect(be.applyChanges(root, 'site', d.draft_id, [], ['nope.html'], 'm', {})).rejects.toThrow(/FILE_NOT_FOUND/);
  });

  it('deletes a file', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    const r = await be.applyChanges(root, 'site', d.draft_id, [], ['index.html'], 'drop', {});
    expect(r.deleted).toEqual(['index.html']);
    expect((await be.listFiles(root, 'site', { draftId: d.draft_id })).some(f => f.path === 'index.html')).toBe(false);
  });
});

describe('readFile', () => {
  it('returns FILE_NOT_FOUND for a missing path', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await expect(be.readFile(root, 'site', { draftId: d.draft_id }, 'nope.txt')).rejects.toThrow(/FILE_NOT_FOUND/);
  });

  it('flags a non-UTF-8 file as binary instead of corrupting it', async () => {
    // Build the fixture through the PUBLIC path: import a Latin-1 file at init.
    const src2 = await mkdtemp(path.join(tmpdir(), 'vf-bin-'));
    await writeFile(path.join(src2, 'l1.txt'), Buffer.from([0xe9, 0xe8, 0xfc]));
    const r = await be.init(root, 'binfolder', { files: await readSource(src2) });
    const f = await be.readFile(root, 'binfolder', { versionId: r.current_version }, 'l1.txt');
    // A NUL-only heuristic would call this text and hand back replacement characters.
    expect(f.encoding).toBe('binary');
    expect(f.content).toBeNull();
    await rm(src2, { recursive: true, force: true });
  });
});

describe('diff', () => {
  it('reports counts and per-file status against base', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await be.applyChanges(root, 'site', d.draft_id,
      [{ path: 'index.html', content: '<h1>v2</h1>\nmore\n' }, { path: 'new.txt', content: 'n\n' }], [], 'm', {});
    const r = await be.diff(root, 'site', d.draft_id, 'base');
    expect(r.files_changed).toBe(2);
    expect(r.insertions).toBeGreaterThan(0);
    expect(r.files).toEqual(expect.arrayContaining([
      { path: 'index.html', status: 'modified' }, { path: 'new.txt', status: 'added' },
    ]));
  });

  it('is empty for an untouched draft', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    const r = await be.diff(root, 'site', d.draft_id, 'base');
    expect(r.files_changed).toBe(0);
    expect(r.files).toEqual([]);
  });

  it('truncates a huge diff and says so', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await be.applyChanges(root, 'site', d.draft_id, [{ path: 'big.txt', content: 'line\n'.repeat(50_000) }], [], 'm', {});
    const r = await be.diff(root, 'site', d.draft_id, 'base', { maxDiffBytes: 1024 });
    expect(r.truncated).toBe(true);
    expect(r.diff.length).toBeLessThanOrEqual(1024 + 200);
  });
});

describe('finalize', () => {
  it('freezes the draft into an immutable version and removes the draft', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await be.applyChanges(root, 'site', d.draft_id, [{ path: 'a.txt', content: 'a' }], [], 'm', {});
    const r = await be.finalize(root, 'site', d.draft_id, 'done');
    expect(r.source_draft).toBe(d.draft_id);
    await expect(be.listFiles(root, 'site', { draftId: d.draft_id })).rejects.toThrow(/DRAFT_NOT_FOUND/);
    expect((await be.listFiles(root, 'site', { versionId: r.version_id })).some(f => f.path === 'a.txt')).toBe(true);
  });

  it('rejects finalizing a draft with no changes', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await expect(be.finalize(root, 'site', d.draft_id, 'nothing')).rejects.toThrow(/DRAFT_HAS_NO_CHANGES/);
  });

  it('does not move current', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await be.applyChanges(root, 'site', d.draft_id, [{ path: 'a.txt', content: 'a' }], [], 'm', {});
    await be.finalize(root, 'site', d.draft_id, 'done');
    expect((await be.getCurrent(root, 'site')).current_version).toBe(v1);
  });

  // Teardown is three operations, so failure can strike after any one of them.
  // Inject at every step, not just the first: the bug this replaces only showed
  // up when an EARLIER step had already succeeded.
  for (const failAt of ['worktree', 'branch', 'baseRef']) {
    it(`keeps the finalized version and resumes teardown when it fails at ${failAt}`, async () => {
      const failing = createBackend({ hooks: { afterTeardownStep: (step) => { if (step === failAt) throw new Error('injected'); } } });
      const d = await failing.createDraft(root, 'site', 'current', 'x');
      await failing.applyChanges(root, 'site', d.draft_id, [{ path: 'a.txt', content: 'a' }], [], 'm', {});
      // Identify the new version by SET DIFFERENCE, never by position. The sort is the
      // version id, whose timestamp has one-second resolution, so two versions minted in
      // the same second tie on everything but a random suffix and `after[0]` is then
      // arbitrary. That is a flake that reproduces on a fast machine and nowhere else.
      const beforeIds = new Set((await be.listVersions(root, 'site')).map(v => v.version_id));

      await expect(failing.finalize(root, 'site', d.draft_id, 'done')).rejects.toThrow(/FINALIZE_FAILED/);

      // The snapshot SURVIVES: deleting it would lose the only immutable copy
      // once the worktree or branch is already gone.
      const after = await be.listVersions(root, 'site');
      const created = after.map(v => v.version_id).filter(id => !beforeIds.has(id));
      expect(created).toHaveLength(1);
      // ...and the draft is finalized, not half-open: no further writes.
      await expect(be.applyChanges(root, 'site', d.draft_id, [{ path: 'b.txt', content: 'b' }], [], 'm', {}))
        .rejects.toThrow(/DRAFT_NOT_FOUND/);

      // A retry converges: same version id, teardown completed, marker gone.
      const again = await be.finalize(root, 'site', d.draft_id, 'done');
      expect(again.version_id).toBe(created[0]);
      expect((await be.listVersions(root, 'site')).length).toBe(beforeIds.size + 1);
      expect(await draftDirs(root, 'site')).not.toContain(d.draft_id);
    });
  }

  it('resumes a partial discard rather than stranding the draft', async () => {
    const failing = createBackend({ hooks: { afterTeardownStep: (step) => { if (step === 'branch') throw new Error('injected'); } } });
    const d = await failing.createDraft(root, 'site', 'current', 'x');
    await expect(failing.discardDraft(root, 'site', d.draft_id)).rejects.toThrow();
    await expect(be.discardDraft(root, 'site', d.draft_id)).resolves.toBeTruthy();
    expect(await draftDirs(root, 'site')).not.toContain(d.draft_id);
  });

  it('what you finalize is what you publish', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await be.applyChanges(root, 'site', d.draft_id, [{ path: 'a.txt', content: 'exact-bytes' }], [], 'm', {});
    const { version_id } = await be.finalize(root, 'site', d.draft_id, 'done');
    const before = await be.listFiles(root, 'site', { versionId: version_id });
    await be.publish(root, 'site', version_id, v1);
    expect(await be.listFiles(root, 'site', { versionId: version_id })).toEqual(before);
    expect((await be.readFile(root, 'site', { versionId: version_id }, 'a.txt')).content).toBe('exact-bytes');
  });
});

describe('interrupted draft creation', () => {
  // The compensating `catch` in createDraft covers a FAILED base-ref write. It
  // does not cover the process dying between the two — and the caller never got
  // a draft_id, so nothing else can ever name the orphan. These pin the
  // reclamation, not the catch.
  const orphan = async () => {
    const before = new Set(await draftDirs(root, 'site'));
    // Simulate the crash: the worktree lands, the base ref never does.
    const be2 = createBackend({ hooks: { afterWorktree: () => { throw new Error('killed'); },
                                         beforeTeardown: () => { throw new Error('cleanup died too'); } } });
    await expect(be2.createDraft(root, 'site', 'current', 'x')).rejects.toThrow();
    // Identify the orphan by SET DIFFERENCE, never by position: a caller may
    // already hold a healthy draft, and draftDirs() is sorted, so `left[0]`
    // would hand back the healthy one and the test would reclaim the wrong draft.
    const created = (await draftDirs(root, 'site')).filter((id) => !before.has(id));
    expect(created).toHaveLength(1);       // the orphan really is on disk
    return created[0];
  };

  it('reports an interrupted creation as incomplete, never as a usable draft', async () => {
    const id = await orphan();
    expect(await be.draftState(root, 'site', id)).toBe('incomplete');
    await expect(be.applyChanges(root, 'site', id, [{ path: 'a.txt', content: 'a' }], [], 'm', {}))
      .rejects.toThrow(/DRAFT_NOT_FOUND/);
  });

  it('reclaims the orphan and only the orphan, leaving a fresh draft usable', async () => {
    // The service is what calls reconcileDrafts before createDraft (Task 7); at
    // this layer the two are separate calls, which is also the ordering the
    // service uses.
    const id = await orphan();
    expect(await be.reconcileDrafts(root, 'site')).toEqual([id]);
    const d = await be.createDraft(root, 'site', 'current', 'x');
    const left = await draftDirs(root, 'site');
    expect(left).toEqual([d.draft_id]);                       // orphan gone
    expect(await be.draftState(root, 'site', id)).toBe('missing');
    expect(await be.draftState(root, 'site', d.draft_id)).toBe('open');   // the new one survived
  });

  it('leaves a healthy draft alone when reconciling', async () => {
    const keep = await be.createDraft(root, 'site', 'current', 'x');
    await orphan();
    expect((await be.reconcileDrafts(root, 'site')).length).toBe(1);
    expect(await be.draftState(root, 'site', keep.draft_id)).toBe('open');
    expect((await be.reconcileDrafts(root, 'site')).length).toBe(0);      // idempotent
  });
});

describe('crash recovery (PRD §48)', () => {
  it('restores a dirty worktree to the draft HEAD', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await be.applyChanges(root, 'site', d.draft_id, [{ path: 'a.txt', content: 'committed' }], [], 'm', {});
    const wt = be.getDraftPath(root, 'site', d.draft_id);
    await writeFile(path.join(wt, 'a.txt'), 'HALF WRITTEN');
    await writeFile(path.join(wt, 'stray.txt'), 'orphan');
    expect(await be.recoverDraft(root, 'site', d.draft_id)).toEqual({ recovered: true });
    expect((await be.readFile(root, 'site', { draftId: d.draft_id }, 'a.txt')).content).toBe('committed');
    await expect(stat(path.join(wt, 'stray.txt'))).rejects.toThrow();
  });

  it('removes an ignored stray file too', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await be.applyChanges(root, 'site', d.draft_id, [{ path: '.gitignore', content: 'junk\n' }], [], 'm', {});
    const wt = be.getDraftPath(root, 'site', d.draft_id);
    await writeFile(path.join(wt, 'junk'), 'x');
    await be.recoverDraft(root, 'site', d.draft_id);
    // `git clean -fd` leaves ignored files; -fdx is required for determinism.
    await expect(stat(path.join(wt, 'junk'))).rejects.toThrow();
  });

  it('is a no-op on a clean draft', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    expect(await be.recoverDraft(root, 'site', d.draft_id)).toEqual({ recovered: false });
  });
});

describe('discardDraft', () => {
  it('removes the draft and its directory', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    expect(await be.discardDraft(root, 'site', d.draft_id)).toEqual({ draft_id: d.draft_id, discarded: true });
    await expect(be.listFiles(root, 'site', { draftId: d.draft_id })).rejects.toThrow(/DRAFT_NOT_FOUND/);
  });
});
