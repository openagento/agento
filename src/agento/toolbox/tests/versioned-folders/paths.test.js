import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, mkdir, writeFile, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { safeJoin, safeResolve, validateFolderCode, validateDraftId, validateVersionId } from '../../../modules/versioned_folders/toolbox/paths.js';

const ROOT = '/srv/versioned-folders/site/worktrees/d-a83f21';

describe('safeJoin — syntactic containment', () => {
  it.each([
    '../etc/passwd', '../../etc/passwd', 'a/../../etc/passwd',
    // A raw ".." segment is rejected even when it normalizes back inside the
    // root: the Global Constraint bans the segment, not merely the escape.
    'deep/nested/../file.txt', 'a/./../b',
    '/etc/passwd', '/absolute/path',
    '.git', '.git/config', '.git/hooks/pre-commit',
    '', '.', '..',
  ])('rejects %j', (bad) => { expect(() => safeJoin(ROOT, bad)).toThrow(/INVALID_PATH|PATH_OUTSIDE_FOLDER/); });

  it.each([
    ['index.html', `${ROOT}/index.html`],
    ['css/style.css', `${ROOT}/css/style.css`],
    ['./index.html', `${ROOT}/index.html`],
    ['a/b/c.txt', `${ROOT}/a/b/c.txt`],
    ['nested.git/x', `${ROOT}/nested.git/x`],
  ])('accepts %j', (good, expected) => { expect(safeJoin(ROOT, good)).toBe(expected); });

  it('rejects a sibling sharing the root prefix', () => {
    expect(() => safeJoin(ROOT, '../d-a83f21-evil/x')).toThrow(/INVALID_PATH|PATH_OUTSIDE_FOLDER/);
  });
});

describe('safeResolve — symlink-aware containment', () => {
  let root, outside;
  beforeEach(async () => {
    root = await mkdtemp(path.join(tmpdir(), 'vf-root-'));
    outside = await mkdtemp(path.join(tmpdir(), 'vf-out-'));
    await writeFile(path.join(outside, 'secret.txt'), 'secret');
  });
  afterEach(async () => { await rm(root, {recursive:true,force:true}); await rm(outside, {recursive:true,force:true}); });

  it('rejects a write through a symlinked PARENT directory', async () => {
    await symlink(outside, path.join(root, 'link'));
    await expect(safeResolve(root, 'link/out.txt', { allowSymlinks: false })).rejects.toThrow(/SYMLINK_NOT_ALLOWED/);
  });

  it('rejects a symlinked leaf', async () => {
    await symlink(path.join(outside, 'secret.txt'), path.join(root, 'leak.txt'));
    await expect(safeResolve(root, 'leak.txt', { allowSymlinks: false })).rejects.toThrow(/SYMLINK_NOT_ALLOWED/);
  });

  it('rejects a deeply nested symlinked ancestor', async () => {
    await mkdir(path.join(root, 'a/b'), { recursive: true });
    await symlink(outside, path.join(root, 'a/b/c'));
    await expect(safeResolve(root, 'a/b/c/d/e.txt', { allowSymlinks: false })).rejects.toThrow(/SYMLINK_NOT_ALLOWED/);
  });

  it('STILL rejects a symlink when allowSymlinks is true (unsupported in MVP)', async () => {
    // allowSymlinks:true must never become a containment bypass. MVP rejects the
    // configuration itself (see the service test); safeResolve stays strict so a
    // mis-wired caller cannot escape either.
    await symlink(outside, path.join(root, 'link'));
    await expect(safeResolve(root, 'link/out.txt', { allowSymlinks: true })).rejects.toThrow(/SYMLINK_NOT_ALLOWED/);
  });

  it('accepts a plain nested path that does not yet exist', async () => {
    await expect(safeResolve(root, 'new/dir/file.txt', { allowSymlinks: false }))
      .resolves.toBe(path.join(root, 'new/dir/file.txt'));
  });

  it('still rejects traversal', async () => {
    await expect(safeResolve(root, '../x', { allowSymlinks: false })).rejects.toThrow(/INVALID_PATH/);
  });
});

describe('identifier validation', () => {
  it.each(['openagento-website', 'a', 'monthly-sales-report'])('accepts folder_code %j', (c) => {
    expect(validateFolderCode(c)).toBe(c);
  });
  it.each(['../evil', '/abs', 'Has-Upper', 'has_underscore', '-leading', '', 'x'.repeat(65), 'a/b'])(
    'rejects folder_code %j', (c) => { expect(() => validateFolderCode(c)).toThrow(/INVALID_PATH/); });
  it('validates draft and version ids', () => {
    expect(validateDraftId('d-a83f21')).toBe('d-a83f21');
    expect(() => validateDraftId('d-../..')).toThrow(/INVALID_PATH/);
    expect(validateVersionId('v-20260905-154012-a3f2')).toBe('v-20260905-154012-a3f2');
    expect(() => validateVersionId('current')).toThrow(/INVALID_PATH/);
  });
});
