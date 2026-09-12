import { readdir, readFile, lstat, writeFile } from 'node:fs/promises';
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

// A version as a FIXTURE. Minting one through `save_version` needs a desk, which is
// fd-anchored and Linux-only, and neither the published tree nor the CAS cares how the
// version it is handed was made. The id is stamped from the clock like a real one, one
// second apart per call: retention orders by NAME, so an id dated in the past would
// sort older than the version `init` just minted and the ordering under test would be
// fiction.
let minted = 0;
export async function mintVersion(be, root, artifact, content) {
  const draft = await be.createDraft(root, artifact, 'current', 'x');
  await writeFile(path.join(be.getDraftPath(root, artifact, draft.draft_id), 'index.html'), content);
  const { commit } = await be.commitDraft(root, artifact, draft.draft_id, 'm');
  const stamp = new Date(Date.now() + ++minted * 1000).toISOString().replace(/[-:T]/g, '');
  const versionId = `v-${stamp.slice(0, 8)}-${stamp.slice(8, 14)}-f${String(minted % 1000).padStart(3, '0')}`;
  git(root, artifact, ['update-ref', `refs/agento/versions/${versionId}`, commit]);
  await be.discardDraft(root, artifact, draft.draft_id);
  return versionId;
}

const git = (root, artifact, args) =>
  execFileSync('git', ['--git-dir', path.join(root, artifact, 'repo.git'), ...args], { encoding: 'utf8' }).trim();

// The commit message of a saved version — inspection, not a module export.
export const lastMessage = (root, artifact, versionId) => git(root, artifact, ['log', '-1', '--format=%B', `refs/agento/versions/${versionId}`]);

// The draft ids currently present, read from the store rather than from the API.
export const draftDirs = async (root, artifact) =>
  git(root, artifact, ['for-each-ref', '--format=%(refname:lstrip=3)', 'refs/heads/agento-drafts/'])
    .split('\n').filter(Boolean).map(r => r.replace('agento-drafts/', '')).sort();
