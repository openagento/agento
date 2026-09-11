import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, mkdir, writeFile, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createBackend } from '../../../modules/versioned_folders/toolbox/git-backend.js';
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

const mkVersion = async (folder, content) => {
  const d = await be.createDraft(root, folder, 'current', 'x');
  await be.applyChanges(root, folder, d.draft_id, [{ path: 'a.txt', content }], [], 'm', {});
  return (await be.finalize(root, folder, d.draft_id, 'v')).version_id;
};

describe('init', () => {
  it('imports a source dir, creates version 1, and sets it current', async () => {
    const r = await be.init(root, 'site', { files: await readSource(src) });
    expect(r.current_version).toMatch(/^v-\d{8}-\d{6}-[a-z0-9]{4}$/);
    expect((await be.getCurrent(root, 'site')).current_version).toBe(r.current_version);
    expect(await be.listVersions(root, 'site')).toHaveLength(1);
  });

  it('creates an empty folder when no source is given', async () => {
    expect((await be.init(root, 'empty', {})).current_version).toMatch(/^v-/);
  });

  it('refuses to re-init an existing folder with GIT_OPERATION_FAILED', async () => {
    await be.init(root, 'site', { files: await readSource(src) });
    const err = await be.init(root, 'site', { files: await readSource(src) }).catch(e => e);
    expect(err.code).toBe('GIT_OPERATION_FAILED');
    expect(err.detail).toMatch(/already exists/);
  });

  it('leaves NO folder behind when import fails, so a retry can succeed', async () => {
    await symlink('/etc/passwd', path.join(src, 'link.txt'));
    await expect(be.init(root, 'site', { files: await readSource(src) })).rejects.toThrow(/SYMLINK_NOT_ALLOWED/);
    await rm(path.join(src, 'link.txt'));
    // A half-created folder would make every retry fail with "already exists".
    await expect(be.init(root, 'site', { files: await readSource(src) })).resolves.toHaveProperty('current_version');
  });

  it('does not import the source .git directory', async () => {
    await mkdir(path.join(src, '.git'), { recursive: true });
    await writeFile(path.join(src, '.git/config'), 'secret');
    const r = await be.init(root, 'site', { files: await readSource(src) });
    expect((await be.listFiles(root, 'site', { versionId: r.current_version })).some(f => f.path.startsWith('.git'))).toBe(false);
  });

  it('refuses to import a symlink', async () => {
    await symlink('/etc/passwd', path.join(src, 'link.txt'));
    await expect(be.init(root, 'site', { files: await readSource(src) })).rejects.toThrow(/SYMLINK_NOT_ALLOWED/);
  });

  it('imports a file a source .gitignore would exclude', async () => {
    await writeFile(path.join(src, '.gitignore'), 'wanted.txt\n');
    await writeFile(path.join(src, 'wanted.txt'), 'keep me');
    const r = await be.init(root, 'site', { files: await readSource(src) });
    expect((await be.listFiles(root, 'site', { versionId: r.current_version })).map(f => f.path)).toContain('wanted.txt');
  });
});

describe('version refs', () => {
  it('never overwrites an existing version on an id collision', async () => {
    // Pin the generator to ONE id, so every finalize asks for the same version.
    be = createBackend({ newVersionId: () => 'v-20260905-154012-aaaa' });
    await be.init(root, 'site', { files: await readSource(src) });
    const first = (await be.getCurrent(root, 'site')).current_version;
    expect(first).toBe('v-20260905-154012-aaaa');
    const firstRevision = (await be.listVersions(root, 'site')).find(v => v.version_id === first).revision;
    // The next finalize must NOT move the existing ref; with retries exhausted it errors.
    await expect(mkVersion('site', 'two')).rejects.toThrow(/VERSION_ALREADY_EXISTS/);
    expect((await be.listVersions(root, 'site')).find(v => v.version_id === first).revision).toBe(firstRevision);
  });

  it('retries a transient collision and succeeds with a different id', async () => {
    await be.init(root, 'site', { files: await readSource(src) });
    const taken = await mkVersion('site', 'one');
    // A test-local sequence: the first ask returns the id already taken, every
    // later ask returns a fresh one. `newVersionId` is a CONSTRUCTOR parameter,
    // not part of the returned operation set, so it cannot be read back off a
    // backend instance — reading it would return undefined and the injected
    // generator below would silently be the only generator in the test.
    let call = 0;
    const seq = () => (++call === 1 ? taken : `v-20260905-154012-${String(call).padStart(4, '0')}`);
    be = createBackend({ newVersionId: seq });
    const second = await mkVersion('site', 'two');
    expect(second).not.toBe(taken);
    expect((await be.listVersions(root, 'site')).map(v => v.version_id)).toContain(taken);  // untouched
  });

  it('does not report VERSION_ALREADY_EXISTS for a non-collision failure', async () => {
    // Count the id requests. Both `init` and `finalize` mint ids, so the counter
    // is reset to 0 after init (below): from that point `newVersionId` can only
    // be called by finalize's version-ref transaction, and a non-zero count is
    // positive proof the failure happened AT the version-ref write. Without it
    // this test passes just as well when finalize dies earlier, which is the
    // whole defect it exists to rule out.
    let asked = 0;
    be = createBackend({ newVersionId: () => `v-20260905-154012-${String(++asked).padStart(4, '0')}` });
    await be.init(root, 'site', { files: await readSource(src) });
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await be.applyChanges(root, 'site', d.draft_id, [{ path: 'a.txt', content: 'two' }], [], 'm', {});
    asked = 0;
    // Break ONLY the versions namespace. Removing all of `refs/` would take the
    // draft branch and `refs/agento/draft-bases/<id>` with it, so finalize would
    // fail while resolving the draft — before the version-ref write ever runs — and
    // every assertion below would hold for the wrong reason.
    const versionsDir = path.join(root, 'site/repo.git/refs/agento/versions');
    await rm(versionsDir, { recursive: true, force: true });
    await writeFile(versionsDir, 'not a directory');   // update-ref now fails with ENOTDIR
    const err = await be.finalize(root, 'site', d.draft_id, 'x').catch(e => e);
    expect(asked).toBeGreaterThan(0);                  // the version-ref write WAS reached
    expect(err.name).toBe('VfError');
    expect(err.code).not.toBe('VERSION_ALREADY_EXISTS');
    expect(err.code).toBe('GIT_OPERATION_FAILED');
    // The draft survived a failed finalize: no marker, still writable.
    expect(await be.draftState(root, 'site', d.draft_id)).toBe('open');
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
    expect((await be.listFiles(root, 'site', { versionId: v1 })).some(f => f.path === 'a.txt')).toBe(false);
  });

  it('resolves current to exactly one version id', async () => {
    const { current_version: v1 } = await be.init(root, 'site', { files: await readSource(src) });
    const v2 = await mkVersion('site', 'two');
    await be.publish(root, 'site', v2, v1);
    const revisions = (await be.listVersions(root, 'site')).map(v => v.revision);
    expect(new Set(revisions).size).toBe(revisions.length);
    expect((await be.getCurrent(root, 'site')).current_version).toBe(v2);
  });

  it('rejects an unknown version and an unknown folder', async () => {
    const { current_version: v1 } = await be.init(root, 'site', { files: await readSource(src) });
    await expect(be.publish(root, 'site', 'v-20200101-000000-dead', v1)).rejects.toThrow(/VERSION_NOT_FOUND/);
    await expect(be.getCurrent(root, 'nope')).rejects.toThrow(/FOLDER_NOT_FOUND/);
  });

  it('listVersions reports revision and honours limit', async () => {
    await be.init(root, 'site', { files: await readSource(src) });
    await mkVersion('site', 'two');
    expect((await be.listVersions(root, 'site'))[0]).toHaveProperty('revision');
    expect(await be.listVersions(root, 'site', { limit: 1 })).toHaveLength(1);
  });
});
