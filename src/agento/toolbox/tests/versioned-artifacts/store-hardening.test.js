import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, readdir, readFile, writeFile, unlink } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createBackend } from '../../../modules/versioned_artifacts/toolbox/git-backend.js';

let root, be;
beforeEach(async () => {
  be = createBackend();
  root = await mkdtemp(path.join(tmpdir(), 'va-harden-'));
});
afterEach(async () => { await rm(root, { recursive: true, force: true }); });

const repo = (code) => path.join(root, code, 'repo.git');
const attrs = (code) => path.join(repo(code), 'info', 'attributes');
// The bytes Git actually STORED, which is what every reading path must reproduce.
const storedBlob = (code, ref, file) =>
  execFileSync('git', ['--git-dir', repo(code), 'cat-file', 'blob', `${ref}:${file}`]);

describe('bare repo creation', () => {
  it('ships no hook samples', async () => {
    await be.init(root, 'site', {});
    const hooks = await readdir(path.join(repo('site'), 'hooks')).catch(() => []);
    expect(hooks.filter((h) => h.endsWith('.sample'))).toEqual([]);
  });
});

describe('tree-controlled Git attributes are neutralized', () => {
  const LINE = '* -export-ignore -export-subst -text -eol -filter !diff -ident !working-tree-encoding';

  it('a fresh artifact carries the neutralization line', async () => {
    await be.init(root, 'site', {});
    expect((await readFile(attrs('site'), 'utf8')).trim()).toBe(LINE);
  });

  it('restores the line on the next ordinary operation after it is deleted', async () => {
    await be.init(root, 'site', {});
    await unlink(attrs('site'));

    await be.getCurrent(root, 'site');

    expect((await readFile(attrs('site'), 'utf8')).trim()).toBe(LINE);
  });

  it('restores the line after it is altered', async () => {
    await be.init(root, 'site', {});
    await writeFile(attrs('site'), '* text eol=crlf\n');

    await be.getCurrent(root, 'site');

    expect((await readFile(attrs('site'), 'utf8')).trim()).toBe(LINE);
  });

  it('checks a draft out byte-identically despite an in-tree .gitattributes', async () => {
    await be.init(root, 'site', {
      files: [
        { path: '.gitattributes', content: '* text eol=crlf\nident.txt ident\n' },
        { path: 'a.txt', content: 'a\nb\n' },
        { path: 'ident.txt', content: '$Id$\n' },
      ],
    });

    const d = await be.createDraft(root, 'site', 'current', 'x');
    const worktree = path.join(root, 'site', 'worktrees', d.draft_id);

    expect(await readFile(path.join(worktree, 'a.txt'))).toEqual(storedBlob('site', 'refs/agento/current', 'a.txt'));
    expect(await readFile(path.join(worktree, 'ident.txt'))).toEqual(storedBlob('site', 'refs/agento/current', 'ident.txt'));
  });

  it('diffs as text despite an in-tree .gitattributes marking files binary', async () => {
    await be.init(root, 'site', {
      files: [{ path: '.gitattributes', content: '* binary\n' }, { path: 'a.txt', content: 'one\n' }],
    });
    const d = await be.createDraft(root, 'site', 'current', 'x');
    await writeFile(path.join(root, 'site', 'worktrees', d.draft_id, 'a.txt'), 'two\n');
    await be.commitDraft(root, 'site', d.draft_id, 'm');

    const diff = await be.diff(root, 'site', d.draft_id, 'current');

    expect(JSON.stringify(diff)).not.toContain('Binary files');
    expect(JSON.stringify(diff)).toContain('two');
  });
});

describe('the neutralization line is not reachable from the tree', () => {
  it('an agent-committed .gitattributes cannot override $GIT_DIR/info/attributes', async () => {
    await be.init(root, 'site', {
      files: [{ path: '.gitattributes', content: '* text eol=crlf\n' }, { path: 'a.txt', content: 'a\nb\n' }],
    });
    const d = await be.createDraft(root, 'site', 'current', 'x');
    const worktree = path.join(root, 'site', 'worktrees', d.draft_id);

    // Resolved in the worktree, where the agent's .gitattributes really is
    // checked out, so this is the full precedence chain the reading paths walk.
    const resolved = execFileSync('git', ['-C', worktree, 'check-attr', 'text', '--', 'a.txt'],
      { encoding: 'utf8' });

    expect(resolved).toContain('text: unset');
  });
});
