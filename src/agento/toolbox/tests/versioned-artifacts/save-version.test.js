import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm } from 'node:fs/promises';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createBackend } from '../../../modules/versioned_artifacts/toolbox/git-backend.js';
import { ERROR_CODES } from '../../../modules/versioned_artifacts/toolbox/errors.js';

// Linux-gated as a whole: `saveVersion` reads the desk through `mirrorOut`, which is
// fd-anchored and calls `requireLinux()`.
const linux = process.platform === 'linux';

let root, be, desk, deskFd, v1, d;
const opened = [];
beforeEach(async () => {
  be = createBackend();
  root = await mkdtemp(path.join(tmpdir(), 'va-save-'));
  desk = await mkdtemp(path.join(tmpdir(), 'va-desk-'));
  ({ current_version: v1 } = await be.init(root, 'site', { files: [{ path: 'index.html', content: 'v1\n' }] }));
  d = await be.createDraft(root, 'site', 'current', 'x');
  if (linux) {
    deskFd = fs.openSync(desk, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY);
    opened.push(deskFd);
    // The desk starts as the agent would find it: a materialized copy of the draft.
    await be.materialize(root, 'site', { draftId: d.draft_id }, deskFd);
  }
});
afterEach(async () => {
  while (opened.length) { try { fs.closeSync(opened.pop()); } catch { /* already closed */ } }
  await rm(root, { recursive: true, force: true });
  await rm(desk, { recursive: true, force: true });
});

const repo = () => path.join(root, 'site', 'repo.git');
const git = (...args) => execFileSync('git', ['--git-dir', repo(), ...args], { encoding: 'utf8' }).trim();
const versionFile = (versionId, file) =>
  execFileSync('git', ['--git-dir', repo(), 'cat-file', 'blob', `refs/agento/versions/${versionId}:${file}`], { encoding: 'utf8' });
const put = (rel, body) => {
  fs.mkdirSync(path.dirname(path.join(desk, rel)), { recursive: true });
  fs.writeFileSync(path.join(desk, rel), body);
};
const worktree = () => be.getDraftPath(root, 'site', d.draft_id);

describe.skipIf(!linux)('save_version', () => {
  it('saves the desk as a new version and leaves the draft open for a second save', async () => {
    put('index.html', 'v2\n');
    const a = await be.saveVersion(root, 'site', d.draft_id, deskFd, { description: 'second' });

    put('index.html', 'v3\n');
    const b = await be.saveVersion(root, 'site', d.draft_id, deskFd, { description: 'third' });

    expect(a.version_id).not.toBe(b.version_id);
    expect(versionFile(a.version_id, 'index.html')).toBe('v2\n');
    expect(versionFile(b.version_id, 'index.html')).toBe('v3\n');
    // The draft survives BOTH saves — the teardown is what phase 2 removes.
    expect(await be.draftState(root, 'site', d.draft_id)).toBe('open');
    // The draft head IS version b, so diffing the draft against a shows what the second
    // save changed — which is what a still-open draft makes possible at all.
    const changed = await be.diff(root, 'site', d.draft_id, a.version_id);
    expect(changed.files.map((f) => f.path)).toEqual(['index.html']);
  });

  it('carries a file deleted from the desk into the version', async () => {
    put('extra.txt', 'x');
    const a = await be.saveVersion(root, 'site', d.draft_id, deskFd, {});
    expect(versionFile(a.version_id, 'extra.txt')).toBe('x');

    fs.unlinkSync(path.join(desk, 'extra.txt'));
    const b = await be.saveVersion(root, 'site', d.draft_id, deskFd, {});

    expect(() => versionFile(b.version_id, 'extra.txt')).toThrow();
    expect(versionFile(b.version_id, 'index.html')).toBe('v1\n');
  });

  it('saves a desk the agent emptied on purpose, rather than reading it as absence', async () => {
    fs.unlinkSync(path.join(desk, 'index.html'));
    const r = await be.saveVersion(root, 'site', d.draft_id, deskFd, {});

    expect(r.version_id).not.toBe(v1);
    expect(git('ls-tree', '-r', '--name-only', `refs/agento/versions/${r.version_id}`)).toBe('');
  });

  it('returns the version that already holds these bytes when nothing changed', async () => {
    // No error: from content alone a retry after a timeout and a deliberate second save
    // with no edits are the same event, and only one answer is correct for both.
    const r = await be.saveVersion(root, 'site', d.draft_id, deskFd, {});
    expect(r.version_id).toBe(v1);
    expect(git('for-each-ref', '--format=%(refname)', 'refs/agento/versions/').split('\n')).toHaveLength(1);
  });

  it('returns the same id and creates nothing when a completed save is retried', async () => {
    put('index.html', 'v2\n');
    const a = await be.saveVersion(root, 'site', d.draft_id, deskFd, {});
    const b = await be.saveVersion(root, 'site', d.draft_id, deskFd, {});

    expect(b.version_id).toBe(a.version_id);
    expect(git('for-each-ref', '--format=%(refname)', 'refs/agento/versions/').split('\n')).toHaveLength(2);
    expect(git('rev-list', '--count', `refs/heads/agento-drafts/${d.draft_id}`)).toBe('2');
  });

  it('mints the ref for an existing tip when a save died between the commit and the ref', async () => {
    // The crash state, reconstructed through production code: the commit landed, the
    // version ref did not. `commitDraft` is exactly the step `saveVersion` runs.
    put('index.html', 'v2\n');
    fs.writeFileSync(path.join(worktree(), 'index.html'), 'v2\n');
    const crashed = await be.commitDraft(root, 'site', d.draft_id, 'v2');
    expect(crashed.committed).toBe(true);
    expect(git('for-each-ref', '--format=%(refname)', 'refs/agento/versions/').split('\n')).toHaveLength(1);

    const r = await be.saveVersion(root, 'site', d.draft_id, deskFd, {});

    expect(git('rev-parse', `refs/agento/versions/${r.version_id}`)).toBe(crashed.commit);
    expect(git('for-each-ref', '--format=%(refname)', 'refs/agento/versions/').split('\n')).toHaveLength(2);
    // No SECOND commit for the same bytes.
    expect(git('rev-list', '--count', `refs/heads/agento-drafts/${d.draft_id}`)).toBe('2');
  });

  it('keeps the worktree .git, proven by the draft still saving afterwards', async () => {
    put('a.txt', 'a');
    await be.saveVersion(root, 'site', d.draft_id, deskFd, {});
    expect(fs.existsSync(path.join(worktree(), '.git'))).toBe(true);

    put('b.txt', 'b');
    const second = await be.saveVersion(root, 'site', d.draft_id, deskFd, {});
    expect(versionFile(second.version_id, 'b.txt')).toBe('b');
  });

  it('does not copy a .git the agent put on the desk into the worktree', async () => {
    fs.mkdirSync(path.join(desk, '.git'));
    fs.writeFileSync(path.join(desk, '.git', 'config'), '[core]\n');
    put('a.txt', 'a');

    const r = await be.saveVersion(root, 'site', d.draft_id, deskFd, {});

    expect(git('ls-tree', '-r', '--name-only', `refs/agento/versions/${r.version_id}`).split('\n').sort())
      .toEqual(['a.txt', 'index.html']);
    // The worktree's own pointer is a FILE; a copied desk directory would have replaced it.
    expect(fs.statSync(path.join(worktree(), '.git')).isFile()).toBe(true);
  });

  it('refuses a draft that is not open', async () => {
    await be.discardDraft(root, 'site', d.draft_id);
    await expect(be.saveVersion(root, 'site', d.draft_id, deskFd, {}))
      .rejects.toThrow(new RegExp(ERROR_CODES.DRAFT_NOT_FOUND));
  });

  it('does not move current', async () => {
    put('a.txt', 'a');
    await be.saveVersion(root, 'site', d.draft_id, deskFd, { description: 'done' });
    expect((await be.getCurrent(root, 'site')).current_version).toBe(v1);
  });

  it('what you save is what you publish', async () => {
    put('a.txt', 'exact-bytes');
    const { version_id } = await be.saveVersion(root, 'site', d.draft_id, deskFd, { description: 'done' });
    const before = git('ls-tree', '-r', '--name-only', `refs/agento/versions/${version_id}`);
    await be.publish(root, 'site', version_id, v1);
    // Publishing MOVES a pointer; it must not rewrite the version it points at.
    expect(git('ls-tree', '-r', '--name-only', `refs/agento/versions/${version_id}`)).toBe(before);
    expect(versionFile(version_id, 'a.txt')).toBe('exact-bytes');
  });

  describe('limits are counted on the bytes actually read from the desk', () => {
    it('refuses more files than max_files', async () => {
      put('a.txt', 'a'); put('b.txt', 'b');
      await expect(be.saveVersion(root, 'site', d.draft_id, deskFd, { limits: { max_files: 2 } }))
        .rejects.toThrow(new RegExp(ERROR_CODES.TOO_MANY_FILES));
    });

    it('refuses a file over max_file_size', async () => {
      put('big.txt', 'x'.repeat(50));
      await expect(be.saveVersion(root, 'site', d.draft_id, deskFd, { limits: { max_file_size: 10 } }))
        .rejects.toThrow(new RegExp(ERROR_CODES.FILE_TOO_LARGE));
    });

    it('measures size in BYTES, not UTF-16 code units', async () => {
      // 40 emoji = 40 JS "length" but 160 UTF-8 bytes. A length check would pass.
      put('e.txt', '\u{1F642}'.repeat(40));
      await expect(be.saveVersion(root, 'site', d.draft_id, deskFd, { limits: { max_file_size: 100 } }))
        .rejects.toThrow(new RegExp(ERROR_CODES.FILE_TOO_LARGE));
    });

    it('refuses a desk over max_total_size', async () => {
      put('a.txt', 'x'.repeat(30)); put('b.txt', 'y'.repeat(30));
      await expect(be.saveVersion(root, 'site', d.draft_id, deskFd, { limits: { max_total_size: 40 } }))
        .rejects.toThrow(new RegExp(ERROR_CODES.ARTIFACT_TOO_LARGE));
    });

    it('leaves no version behind when a limit is refused', async () => {
      put('big.txt', 'x'.repeat(50));
      await expect(be.saveVersion(root, 'site', d.draft_id, deskFd, { limits: { max_file_size: 10 } }))
        .rejects.toThrow();
      expect(git('for-each-ref', '--format=%(refname)', 'refs/agento/versions/').split('\n')).toHaveLength(1);
    });
  });
});

// The minting half, which used to live in `git-backend-core.test.js`'s `version refs`
// describe: a version can only be minted through a save now, so the tests moved to
// where a desk exists. Their own store and their own backend, because two of them pin
// `newVersionId`, which is a CONSTRUCTOR parameter.
describe.skipIf(!linux)('version refs', () => {
  let vroot, vdesk, vdeskFd;
  const vopened = [];
  const vgit = (...args) => execFileSync('git', ['--git-dir', path.join(vroot, 'site', 'repo.git'), ...args], { encoding: 'utf8' }).trim();
  const seed = async (backend, content) => {
    await backend.init(vroot, 'site', { files: [{ path: 'a.txt', content: 'one\n' }] });
    const draft = await backend.createDraft(vroot, 'site', 'current', 'x');
    await backend.materialize(vroot, 'site', { draftId: draft.draft_id }, vdeskFd);
    fs.writeFileSync(path.join(vdesk, 'a.txt'), content);
    return draft.draft_id;
  };
  beforeEach(async () => {
    vroot = await mkdtemp(path.join(tmpdir(), 'va-vref-'));
    vdesk = await mkdtemp(path.join(tmpdir(), 'va-vdesk-'));
    vdeskFd = fs.openSync(vdesk, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY);
    vopened.push(vdeskFd);
  });
  afterEach(async () => {
    while (vopened.length) { try { fs.closeSync(vopened.pop()); } catch { /* already closed */ } }
    await rm(vroot, { recursive: true, force: true });
    await rm(vdesk, { recursive: true, force: true });
  });

  it('never overwrites an existing version on an id collision', async () => {
    // Pin the generator to ONE id, so every save asks for the same version.
    const pinned = createBackend({ newVersionId: () => 'v-20260905-154012-aaaa' });
    const draftId = await seed(pinned, 'two\n');
    const first = (await pinned.getCurrent(vroot, 'site')).current_version;
    expect(first).toBe('v-20260905-154012-aaaa');
    const firstRevision = (await pinned.listVersions(vroot, 'site')).find(v => v.version_id === first).revision;

    await expect(pinned.saveVersion(vroot, 'site', draftId, vdeskFd, {}))
      .rejects.toThrow(new RegExp(ERROR_CODES.VERSION_ALREADY_EXISTS));

    expect((await pinned.listVersions(vroot, 'site')).find(v => v.version_id === first).revision).toBe(firstRevision);
  });

  it('retries a transient collision and succeeds with a different id', async () => {
    // `init` takes the first id; the save's FIRST ask collides with it and its second
    // gets a fresh one.
    const ids = ['v-20260905-154012-aaaa', 'v-20260905-154012-aaaa', 'v-20260905-154012-0003'];
    let call = 0;
    const retrying = createBackend({ newVersionId: () => ids[Math.min(call++, ids.length - 1)] });
    const draftId = await seed(retrying, 'two\n');
    const taken = (await retrying.getCurrent(vroot, 'site')).current_version;
    expect(taken).toBe('v-20260905-154012-aaaa');

    const saved = await retrying.saveVersion(vroot, 'site', draftId, vdeskFd, {});

    expect(saved.version_id).not.toBe(taken);
    expect((await retrying.listVersions(vroot, 'site')).map(v => v.version_id)).toContain(taken);  // untouched
  });

  it('does not report VERSION_ALREADY_EXISTS for a non-collision failure', async () => {
    // Count the id requests: after the seed, `newVersionId` can only be called by the
    // save's version-ref write, so a non-zero count is positive proof the failure
    // happened THERE and not earlier.
    let asked = 0;
    const counting = createBackend({ newVersionId: () => `v-20260905-154012-${String(++asked).padStart(4, '0')}` });
    const draftId = await seed(counting, 'two\n');
    asked = 0;
    // Break ONLY the versions namespace: removing all of `refs/` would take the draft
    // branch and its base ref with it, and the save would fail before the ref write.
    const versionsDir = path.join(vroot, 'site/repo.git/refs/agento/versions');
    await rm(versionsDir, { recursive: true, force: true });
    fs.writeFileSync(versionsDir, 'not a directory');   // update-ref now fails with ENOTDIR

    const err = await counting.saveVersion(vroot, 'site', draftId, vdeskFd, {}).catch(e => e);

    expect(asked).toBeGreaterThan(0);                   // the version-ref write WAS reached
    expect(err.name).toBe('ArtifactError');
    expect(err.code).not.toBe(ERROR_CODES.VERSION_ALREADY_EXISTS);
    expect(err.code).toBe(ERROR_CODES.STORAGE_OPERATION_FAILED);
    // The draft survived the failed save: still open, still writable.
    expect(await counting.draftState(vroot, 'site', draftId)).toBe('open');
  });

  it('refuses to answer when two versions point at one revision', async () => {
    // Idempotency reads the version back FROM the tip, so "one commit, one version" is
    // load-bearing: nothing in this module may ever point a second version ref at an
    // existing commit. A store where that happened must say so, not pick one.
    const backend = createBackend();
    const draftId = await seed(backend, 'two\n');
    const saved = await backend.saveVersion(vroot, 'site', draftId, vdeskFd, {});
    vgit('update-ref', 'refs/agento/versions/v-20260905-154012-bbbb', `refs/agento/versions/${saved.version_id}`);

    // A save with nothing new to commit is the call that asks the question.
    await expect(backend.saveVersion(vroot, 'site', draftId, vdeskFd, {}))
      .rejects.toThrow(new RegExp(ERROR_CODES.STORAGE_OPERATION_FAILED));
  });
});
