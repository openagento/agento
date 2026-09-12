import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, writeFile, readdir } from 'node:fs/promises';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createBackend } from '../../../modules/versioned_artifacts/toolbox/git-backend.js';
import { ERROR_CODES } from '../../../modules/versioned_artifacts/toolbox/errors.js';

// Linux-gated as a whole: `materialize` writes through `mirrorIn`, which is fd-anchored
// and calls `requireLinux()`. The plan gates only `desk-containment.test.js`; every suite
// whose subject routes through the desk is Linux-only by the same construction.
const linux = process.platform === 'linux';

let root, be, dest, destFd;
const opened = [];
beforeEach(async () => {
  be = createBackend();
  root = await mkdtemp(path.join(tmpdir(), 'va-mat-'));
  dest = await mkdtemp(path.join(tmpdir(), 'va-desk-'));
  if (linux) {
    destFd = fs.openSync(dest, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY);
    opened.push(destFd);
  }
});
afterEach(async () => {
  while (opened.length) { try { fs.closeSync(opened.pop()); } catch { /* already closed */ } }
  await rm(root, { recursive: true, force: true });
  await rm(dest, { recursive: true, force: true });
});

const repo = (code) => path.join(root, code, 'repo.git');
// The bytes Git actually STORED — the only correct target for "byte-identical".
const storedBlob = (code, ref, file) =>
  execFileSync('git', ['--git-dir', repo(code), 'cat-file', 'blob', `${ref}:${file}`]);
const names = async (dir) => (await readdir(dir)).sort();

// Selector validation happens before any I/O, so these two need no desk and no Linux.
describe('materialize selector validation', () => {
  it('refuses a request that names both a draft and a version', async () => {
    await be.init(root, 'site', { files: [{ path: 'a.txt', content: 'a' }] });
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await expect(be.materialize(root, 'site', { draftId: d.draft_id, versionId: 'v-20250101-000000-aaaa' }, 3))
      .rejects.toThrow(new RegExp(ERROR_CODES.INVALID_PATH));
  });

  it('refuses a request that names neither', async () => {
    await be.init(root, 'site', {});
    await expect(be.materialize(root, 'site', {}, 3))
      .rejects.toThrow(new RegExp(ERROR_CODES.INVALID_PATH));
  });

  it('reports an unknown version as VERSION_NOT_FOUND, not as an empty tree', async () => {
    await be.init(root, 'site', {});
    await expect(be.materialize(root, 'site', { versionId: 'v-20250101-000000-zzzz' }, 3))
      .rejects.toThrow(new RegExp(ERROR_CODES.VERSION_NOT_FOUND));
  });
});

// Damage is found while reading the STORE, before any desk write, so this needs
// neither a desk nor Linux.
describe('materialize reports damage, never absence', () => {
  it('maps a missing object to a storage failure instead of a short tree', async () => {
    const { current_version: v1 } = await be.init(root, 'site', { files: [{ path: 'a.txt', content: 'a' }] });
    const sha = execFileSync('git', ['--git-dir', repo('site'), 'rev-parse',
      `refs/agento/versions/${v1}:a.txt`], { encoding: 'utf8' }).trim();
    // The tree still names the path; only the blob is gone. `materialize` is the one
    // path left that reads file CONTENT, so it is where this class now lives.
    await rm(path.join(repo('site'), 'objects', sha.slice(0, 2), sha.slice(2)), { force: true });
    await expect(be.materialize(root, 'site', { versionId: v1 }, 3))
      .rejects.toThrow(new RegExp(ERROR_CODES.STORAGE_OPERATION_FAILED));
  });
});

describe.skipIf(!linux)('materialize', () => {
  it('copies a draft without its .git', async () => {
    await be.init(root, 'site', { files: [{ path: 'index.html', content: '<h1>v1</h1>' }] });
    const d = await be.createDraft(root, 'site', 'current', 'x');

    await be.materialize(root, 'site', { draftId: d.draft_id }, destFd);

    expect(await names(dest)).toEqual(['index.html']);
  });

  it('copies what the draft RECORDS, not a half-written worktree', async () => {
    // The worktree is a staging area: `save_version` mirrors the desk into it and
    // commits under one lock, so the worktree is never legitimately ahead of the draft
    // tip. A worktree that IS ahead is a torn write — a save that died mid-mirror, or a
    // concurrent one in flight — and the recovery path an agent reaches for is exactly
    // this call. It must hand back the recorded draft, never the torn bytes.
    await be.init(root, 'site', { files: [{ path: 'index.html', content: '<h1>v1</h1>' }] });
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await writeFile(path.join(be.getDraftPath(root, 'site', d.draft_id), 'half-written.txt'), 'torn');

    await be.materialize(root, 'site', { draftId: d.draft_id }, destFd);

    expect(await names(dest)).toEqual(['index.html']);
  });

  it('copies a version byte-identically despite an in-tree .gitattributes', async () => {
    // `text eol=crlf` and `ident` are CHECKOUT-affecting: they are what would rewrite
    // the bytes if phase 1's `info/attributes` line were not neutralizing them.
    const { current_version: v1 } = await be.init(root, 'site', {
      files: [
        { path: '.gitattributes', content: '* text eol=crlf\nident.txt ident\n' },
        { path: 'a.txt', content: 'a\nb\n' },
        { path: 'ident.txt', content: '$Id$\n' },
        // Non-UTF-8: a decode-then-write path would hand back replacement characters.
        { path: 'l1.txt', content: Buffer.from([0xe9, 0xe8, 0xfc]).toString('base64'), encoding: 'base64' },
      ],
    });

    await be.materialize(root, 'site', { versionId: v1 }, destFd);

    const ref = `refs/agento/versions/${v1}`;
    for (const f of ['.gitattributes', 'a.txt', 'ident.txt', 'l1.txt']) {
      expect(fs.readFileSync(path.join(dest, f))).toEqual(storedBlob('site', ref, f));
    }
  });

  it('copies a version with nested directories', async () => {
    const { current_version: v1 } = await be.init(root, 'site', {
      files: [{ path: 'css/app.css', content: 'body{}' }, { path: 'index.html', content: 'x' }],
    });

    await be.materialize(root, 'site', { versionId: v1 }, destFd);

    expect(await names(dest)).toEqual(['css', 'index.html']);
    expect(fs.readFileSync(path.join(dest, 'css', 'app.css'), 'utf8')).toBe('body{}');
  });

  it('leaves no temporary behind, and the store still lists exactly one artifact', async () => {
    const { current_version: v1 } = await be.init(root, 'site', { files: [{ path: 'a.txt', content: 'a' }] });

    await be.materialize(root, 'site', { versionId: v1 }, destFd);

    // The scratch tree is derived from storageRoot, so it lands INSIDE it. A leftover
    // would be an unbounded store leak, and a name `listArtifacts` accepted would be a
    // phantom artifact.
    const tmp = path.join(root, '.tmp');
    expect(fs.existsSync(tmp) ? await names(tmp) : []).toEqual([]);
    expect((await be.listArtifacts(root)).map((a) => a.artifact_code ?? a)).toEqual(['site']);
  });

  it('removes its temporary even when the copy to the desk fails', async () => {
    const { current_version: v1 } = await be.init(root, 'site', { files: [{ path: 'a.txt', content: 'a' }] });
    // A closed descriptor fails the DESK half and nothing else: the version is extracted
    // first, so this is the one window a scratch leak can open in. (uid-independent —
    // chmod is no barrier to the root uid this suite runs under in the container.)
    const stale = fs.openSync(dest, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY);
    fs.closeSync(stale);

    await expect(be.materialize(root, 'site', { versionId: v1 }, stale)).rejects.toThrow();

    const tmp = path.join(root, '.tmp');
    expect(fs.existsSync(tmp) ? await names(tmp) : []).toEqual([]);
  });

  it('does not disturb the repository index it reads the version through', async () => {
    const { current_version: v1 } = await be.init(root, 'site', { files: [{ path: 'a.txt', content: 'a' }] });

    await be.materialize(root, 'site', { versionId: v1 }, destFd);

    // A bare repo has no index; writing one there is how a scratch-index mistake shows up.
    expect(fs.existsSync(path.join(repo('site'), 'index'))).toBe(false);
  });
});
