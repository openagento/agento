import { readdir, readFile, lstat } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import path from 'node:path';

// Build an init payload from a directory, the same shape the admin CLI sends.
export async function readSource(dir, base = dir) {
  const out = [];
  for (const e of await readdir(dir, { withFileTypes: true })) {
    if (e.name === '.git') continue;
    const full = path.join(dir, e.name);
    if ((await lstat(full)).isSymbolicLink()) { out.push({ path: path.relative(base, full), symlink: true }); continue; }
    if (e.isDirectory()) out.push(...await readSource(full, base));
    else out.push({ path: path.relative(base, full), content: (await readFile(full)).toString('base64'), encoding: 'base64' });
  }
  return out;
}

const git = (root, folder, args) =>
  execFileSync('git', ['--git-dir', path.join(root, folder, 'repo.git'), ...args], { encoding: 'utf8' }).trim();

// The commit message of a finalized version — inspection, not a module export.
export const lastMessage = (root, folder, versionId) => git(root, folder, ['log', '-1', '--format=%B', `refs/agento/versions/${versionId}`]);

// The draft ids currently present, read from the store rather than from the API.
export const draftDirs = async (root, folder) =>
  git(root, folder, ['for-each-ref', '--format=%(refname:lstrip=3)', 'refs/heads/agento-drafts/'])
    .split('\n').filter(Boolean).map(r => r.replace('agento-drafts/', '')).sort();
