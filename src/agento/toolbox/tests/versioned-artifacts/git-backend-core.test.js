import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, mkdir, writeFile, symlink } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createBackend } from '../../../modules/versioned_artifacts/toolbox/git-backend.js';
import { readSource } from './helpers.js';   // test-local, see Step 1a

let root, src, be;
beforeEach(async () => {
  be = createBackend();                      // real defaults; tests that need a seam build their own
  root = await mkdtemp(path.join(tmpdir(), 'vf-'));
  src = await mkdtemp(path.join(tmpdir(), 'vf-src-'));
  await writeFile(path.join(src, 'index.html'), '<h1>v1</h1>');
  await mkdir(path.join(src, 'css'), { recursive: true });
  await writeFile(path.join(src, 'css/style.css'), 'body{}');
});
afterEach(async () => { await rm(root, {recursive:true,force:true}); await rm(src, {recursive:true,force:true}); });

const git = (artifact, ...args) =>
  execFileSync('git', ['--git-dir', path.join(root, artifact, 'repo.git'), ...args], { encoding: 'utf8' }).trim();
// What a version HOLDS, read from the store rather than through a tool: the module no
// longer lists a tree for anyone, and these tests are about `init` and `publish`.
const treeOf = (artifact, versionId) =>
  git(artifact, 'ls-tree', '-r', '--name-only', `refs/agento/versions/${versionId}`).split('\n').filter(Boolean);

// A second version as a FIXTURE, not through the save path: minting a version now
// requires a desk, and `publish` does not care how the versions it moves between were
// made. The one ref this writes is byte-for-byte what `saveVersion` would have written
// — a version ref at a commit `commitDraft` made. Every test whose SUBJECT is the
// minting lives in `save-version.test.js`, where a real desk exists.
let fixtureSeq = 0;
const mkVersion = async (artifact, content) => {
  const d = await be.createDraft(root, artifact, 'current', 'x');
  await writeFile(path.join(be.getDraftPath(root, artifact, d.draft_id), 'a.txt'), content);
  const { commit } = await be.commitDraft(root, artifact, d.draft_id, 'm');
  const versionId = `v-20260905-1540${String(++fixtureSeq).padStart(2, '0')}-f${String(fixtureSeq).padStart(3, '0')}`;
  git(artifact, 'update-ref', `refs/agento/versions/${versionId}`, commit);
  await be.discardDraft(root, artifact, d.draft_id);
  return versionId;
};

describe('init', () => {
  it('imports a source dir, creates version 1, and sets it current', async () => {
    const r = await be.init(root, 'site', { files: await readSource(src) });
    expect(r.current_version).toMatch(/^v-\d{8}-\d{6}-[a-z0-9]{4}$/);
    expect((await be.getCurrent(root, 'site')).current_version).toBe(r.current_version);
    expect(await be.listVersions(root, 'site')).toHaveLength(1);
  });

  it('creates an empty artifact when no source is given', async () => {
    expect((await be.init(root, 'empty', {})).current_version).toMatch(/^v-/);
  });

  it('refuses to re-init an existing artifact with ARTIFACT_ALREADY_EXISTS', async () => {
    // Its OWN code, not the generic storage failure: the service retries under the next
    // number on this one alone, so matching it on message text would make the retry fire
    // on a disk-full or permission error too.
    await be.init(root, 'site', { files: await readSource(src) });
    const err = await be.init(root, 'site', { files: await readSource(src) }).catch(e => e);
    expect(err.code).toBe('ARTIFACT_ALREADY_EXISTS');
    expect(err.detail).toMatch(/already exists/);
  });

  it('leaves NO artifact behind when import fails, so a retry can succeed', async () => {
    await symlink('/etc/passwd', path.join(src, 'link.txt'));
    await expect(be.init(root, 'site', { files: await readSource(src) })).rejects.toThrow(/SYMLINK_NOT_ALLOWED/);
    await rm(path.join(src, 'link.txt'));
    // A half-created artifact would make every retry fail with "already exists".
    await expect(be.init(root, 'site', { files: await readSource(src) })).resolves.toHaveProperty('current_version');
  });

  it('does not import the source .git directory', async () => {
    await mkdir(path.join(src, '.git'), { recursive: true });
    await writeFile(path.join(src, '.git/config'), 'secret');
    const r = await be.init(root, 'site', { files: await readSource(src) });
    expect(treeOf('site', r.current_version).some(p => p.startsWith('.git'))).toBe(false);
  });

  it('refuses to import a symlink', async () => {
    await symlink('/etc/passwd', path.join(src, 'link.txt'));
    await expect(be.init(root, 'site', { files: await readSource(src) })).rejects.toThrow(/SYMLINK_NOT_ALLOWED/);
  });

  it('imports a file a source .gitignore would exclude', async () => {
    await writeFile(path.join(src, '.gitignore'), 'wanted.txt\n');
    await writeFile(path.join(src, 'wanted.txt'), 'keep me');
    const r = await be.init(root, 'site', { files: await readSource(src) });
    expect(treeOf('site', r.current_version)).toContain('wanted.txt');
  });
});

describe('publish', () => {
  it('moves current and returns the previous version', async () => {
    const { current_version: v1 } = await be.init(root, 'site', { files: await readSource(src) });
    const v2 = await mkVersion('site', 'two');
    expect(await be.publish(root, 'site', v2, v1)).toEqual({ previous_version: v1, current_version: v2 });
    expect((await be.getCurrent(root, 'site')).current_version).toBe(v2);
  });

  it('rejects a stale expected_current_version', async () => {
    const { current_version: v1 } = await be.init(root, 'site', { files: await readSource(src) });
    const v2 = await mkVersion('site', 'two');
    await be.publish(root, 'site', v2, v1);
    await expect(be.publish(root, 'site', v1, v1)).rejects.toThrow(/CURRENT_VERSION_CHANGED/);
  });

  it('exactly one of two simultaneous publishes wins', async () => {
    const { current_version: v1 } = await be.init(root, 'site', { files: await readSource(src) });
    const v2 = await mkVersion('site', 'two');
    const v3 = await mkVersion('site', 'three');
    const results = await Promise.allSettled([
      be.publish(root, 'site', v2, v1),
      be.publish(root, 'site', v3, v1),
    ]);
    expect(results.filter(r => r.status === 'fulfilled')).toHaveLength(1);
    const loser = results.find(r => r.status === 'rejected');
    expect(String(loser.reason)).toMatch(/CURRENT_VERSION_CHANGED/);
    const winner = results.find(r => r.status === 'fulfilled').value.current_version;
    expect((await be.getCurrent(root, 'site')).current_version).toBe(winner);
  });

  it('publishes an older version — rollback is just publish', async () => {
    const { current_version: v1 } = await be.init(root, 'site', { files: await readSource(src) });
    const v2 = await mkVersion('site', 'two');
    await be.publish(root, 'site', v2, v1);
    expect((await be.publish(root, 'site', v1, v2)).current_version).toBe(v1);
    expect(treeOf('site', v1)).not.toContain('a.txt');
  });

  it('resolves current to exactly one version id', async () => {
    const { current_version: v1 } = await be.init(root, 'site', { files: await readSource(src) });
    const v2 = await mkVersion('site', 'two');
    await be.publish(root, 'site', v2, v1);
    const revisions = (await be.listVersions(root, 'site')).map(v => v.revision);
    expect(new Set(revisions).size).toBe(revisions.length);
    expect((await be.getCurrent(root, 'site')).current_version).toBe(v2);
  });

  it('rejects an unknown version and an unknown artifact', async () => {
    const { current_version: v1 } = await be.init(root, 'site', { files: await readSource(src) });
    await expect(be.publish(root, 'site', 'v-20200101-000000-dead', v1)).rejects.toThrow(/VERSION_NOT_FOUND/);
    await expect(be.getCurrent(root, 'nope')).rejects.toThrow(/ARTIFACT_NOT_FOUND/);
  });

  it('listVersions reports revision and honours limit', async () => {
    await be.init(root, 'site', { files: await readSource(src) });
    await mkVersion('site', 'two');
    expect((await be.listVersions(root, 'site'))[0]).toHaveProperty('revision');
    expect(await be.listVersions(root, 'site', { limit: 1 })).toHaveLength(1);
  });
});
