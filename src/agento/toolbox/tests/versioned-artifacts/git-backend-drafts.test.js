import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, writeFile, readFile, stat } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createBackend } from '../../../modules/versioned_artifacts/toolbox/git-backend.js';
import { readSource, draftDirs } from './helpers.js';   // test-local, see Step 1a

let root, src, v1, be;
beforeEach(async () => {
  be = createBackend();
  root = await mkdtemp(path.join(tmpdir(), 'vf-'));
  src = await mkdtemp(path.join(tmpdir(), 'vf-src-'));
  await writeFile(path.join(src, 'index.html'), '<h1>v1</h1>\n');
  ({ current_version: v1 } = await be.init(root, 'site', { files: await readSource(src) }));
});
afterEach(async () => { await rm(root, {recursive:true,force:true}); await rm(src, {recursive:true,force:true}); });

// What a draft RECORDS, read from the store. `-z` keeps a name holding a tab or a
// newline intact, where the default output quotes it.
const draftTree = (artifact, draftId) =>
  execFileSync('git', ['--git-dir', path.join(root, artifact, 'repo.git'),
    'ls-tree', '-r', '--name-only', '-z', `refs/heads/agento-drafts/${draftId}`], { encoding: 'utf8' })
    .split('\0').filter(Boolean);

// A fixture puts files where the agent's desk is mirrored to — the draft worktree — and
// commits them through the same `commitDraft` a save uses, so no desk and no Linux gate.
const put = (d, rel, body) => writeFile(path.join(be.getDraftPath(root, 'site', d.draft_id), rel), body);

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
    await put(a, 'a.txt', 'A');
    await be.commitDraft(root, 'site', a.draft_id, 'a', {});
    expect(draftTree('site', b.draft_id)).not.toContain('a.txt');
    await expect(stat(path.join(be.getDraftPath(root, 'site', b.draft_id), 'a.txt'))).rejects.toThrow();
  });

  it('leaves no orphan worktree when draft setup fails partway', async () => {
    await be.createDraft(root, 'site', 'current', 'x');
    const before = await draftDirs(root, 'site');
    // Force the base-ref write to fail after the worktree exists.
    const failing = createBackend({ hooks: { afterWorktree: () => { throw new Error('injected'); } } });
    await expect(failing.createDraft(root, 'site', 'current', 'y')).rejects.toThrow();
    // A leaked worktree or branch would make the artifact accumulate junk and could
    // collide with a later draft id.
    expect(await draftDirs(root, 'site')).toEqual(before);
  });

  it('rejects unknown draft and unknown base version', async () => {
    await expect(be.diff(root, 'site', 'd-deadbeef', 'base')).rejects.toThrow(/DRAFT_NOT_FOUND/);
    await expect(be.createDraft(root, 'site', 'v-20200101-000000-dead', 'x')).rejects.toThrow(/VERSION_NOT_FOUND/);
  });
});

describe('diff', () => {
  it('reports counts and per-file status against base', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await put(d, 'index.html', '<h1>v2</h1>\nmore\n');
    await put(d, 'new.txt', 'n\n');
    await be.commitDraft(root, 'site', d.draft_id, 'm', {});
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
    await put(d, 'big.txt', 'line\n'.repeat(50_000));
    await be.commitDraft(root, 'site', d.draft_id, 'm', {});
    const r = await be.diff(root, 'site', d.draft_id, 'base', { maxDiffBytes: 1024 });
    expect(r.truncated).toBe(true);
    expect(r.diff.length).toBeLessThanOrEqual(1024 + 200);
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
    await expect(be.commitDraft(root, 'site', id, 'm', {})).rejects.toThrow(/DRAFT_NOT_FOUND/);
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
    await put(d, 'a.txt', 'committed');
    await be.commitDraft(root, 'site', d.draft_id, 'm', {});
    const wt = be.getDraftPath(root, 'site', d.draft_id);
    await writeFile(path.join(wt, 'a.txt'), 'HALF WRITTEN');
    await writeFile(path.join(wt, 'stray.txt'), 'orphan');
    expect(await be.recoverDraft(root, 'site', d.draft_id)).toEqual({ recovered: true });
    expect(await readFile(path.join(wt, 'a.txt'), 'utf8')).toBe('committed');
    await expect(stat(path.join(wt, 'stray.txt'))).rejects.toThrow();
  });

  it('removes an ignored stray file too', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await put(d, '.gitignore', 'junk\n');
    await be.commitDraft(root, 'site', d.draft_id, 'm', {});
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
    expect(await be.draftState(root, 'site', d.draft_id)).toBe('missing');
    expect(await draftDirs(root, 'site')).not.toContain(d.draft_id);
  });

  it('resumes a partial discard rather than stranding the draft', async () => {
    const failing = createBackend({ hooks: { afterTeardownStep: (step) => { if (step === 'branch') throw new Error('injected'); } } });
    const d = await failing.createDraft(root, 'site', 'current', 'x');
    await expect(failing.discardDraft(root, 'site', d.draft_id)).rejects.toThrow();
    await expect(be.discardDraft(root, 'site', d.draft_id)).resolves.toBeTruthy();
    expect(await draftDirs(root, 'site')).not.toContain(d.draft_id);
  });
});

describe('commitDraft', () => {
  // `save_version` step (b), exported on its own so a fixture can put files in a draft
  // and commit them through the SAME code production uses — without a desk, and so
  // without a Linux gate on a test whose subject is not the desk.
  it('commits what is in the worktree and returns the new tip', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await put(d, 'a.txt', 'hello\n');
    const r = await be.commitDraft(root, 'site', d.draft_id, 'msg', { jobId: 'j1', agentView: 'av1' });
    expect(r.committed).toBe(true);
    expect(r.commit).toMatch(/^[0-9a-f]{40}$/);
    const body = execFileSync('git', ['-C', be.getDraftPath(root, 'site', d.draft_id), 'log', '-1', '--format=%B'], { encoding: 'utf8' });
    expect(body).toContain('Agento-Draft: ' + d.draft_id);
    expect(body).toContain('Agento-Job: j1');
    expect(body).toContain('Agento-Agent-View: av1');
  });

  it('commits a file the draft own .gitignore excludes', async () => {
    // Without `add -f` the agent is told the save succeeded and the bytes are absent
    // from the version — the defect the `-f` flag exists for.
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await put(d, '.gitignore', 'secret.txt\n');
    await put(d, 'secret.txt', 'kept\n');
    await be.commitDraft(root, 'site', d.draft_id, 'm', {});
    expect(draftTree('site', d.draft_id)).toContain('secret.txt');
  });

  it('handles a filename containing a tab and a newline', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    const weird = 'we\tird\nname.txt';
    await put(d, weird, 'ok');
    await be.commitDraft(root, 'site', d.draft_id, 'm', {});
    expect(draftTree('site', d.draft_id)).toContain(weird);
  });

  it('reports nothing committed when the worktree matches the tip', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    const first = await be.commitDraft(root, 'site', d.draft_id, 'm', {});
    expect(first.committed).toBe(false);
    // The tip is still the base commit, and it is returned rather than left undefined:
    // item 9 asks `versionIdForCommit` about exactly this value.
    expect(first.commit).toMatch(/^[0-9a-f]{40}$/);
  });

  it('stages a deletion, so a file removed from the worktree leaves the revision', async () => {
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await rm(path.join(be.getDraftPath(root, 'site', d.draft_id), 'index.html'));
    const r = await be.commitDraft(root, 'site', d.draft_id, 'm', {});
    expect(r.committed).toBe(true);
    expect(draftTree('site', d.draft_id)).toEqual([]);
  });

  it('keeps a file staged by a crashed batch out of the revision', async () => {
    // The class the round-3 regression guards: `git add` already ran when a batch
    // failed, so the entry is in the INDEX. `add -A -f -- .` re-syncs the index to the
    // worktree, so an entry with no file behind it is staged as a deletion instead of
    // riding along into the next commit.
    const d = await be.createDraft(root, 'site', 'current', 'x');
    const dir = be.getDraftPath(root, 'site', d.draft_id);
    await writeFile(path.join(dir, 'leaked.txt'), 'staged by a crash\n');
    execFileSync('git', ['-C', dir, 'add', 'leaked.txt']);
    await rm(path.join(dir, 'leaked.txt'));
    await put(d, 'wanted.txt', 'w\n');
    await be.commitDraft(root, 'site', d.draft_id, 'm', {});
    const names = draftTree('site', d.draft_id);
    expect(names).toContain('wanted.txt');
    expect(names).not.toContain('leaked.txt');
  });
});

describe('listOpenDrafts', () => {
  // Item 11's recovery story: a retried job starts with a wiped desk and a fresh agent
  // that does not know its draft_id, so the listing is the only way back to the draft.
  it('reports each open draft with the version it was based on', async () => {
    const a = await be.createDraft(root, 'site', 'current', 'a');
    const b = await be.createDraft(root, 'site', 'current', 'b');

    const open = await be.listOpenDrafts(root, 'site');

    expect(open.map((d) => d.draft_id).sort()).toEqual([a.draft_id, b.draft_id].sort());
    expect(open.every((d) => d.base_version === v1)).toBe(true);
  });

  it('drops a discarded draft and keeps the rest', async () => {
    const a = await be.createDraft(root, 'site', 'current', 'a');
    const b = await be.createDraft(root, 'site', 'current', 'b');
    await be.discardDraft(root, 'site', a.draft_id);

    expect((await be.listOpenDrafts(root, 'site')).map((d) => d.draft_id)).toEqual([b.draft_id]);
  });

  it('is empty for an artifact with no drafts', async () => {
    expect(await be.listOpenDrafts(root, 'site')).toEqual([]);
  });
});
