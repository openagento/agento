import { readdir, readFile, lstat, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { vi } from 'vitest';

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

// A store config over a temp store and published root, with every field the service checks.
export const vaConfig = (root, pub, over = {}) => ({ storage_root: root, published_root: pub, allowed_artifacts: 'site',
  'limits/max_files': 2000, 'limits/max_file_size': 5242880, 'limits/max_total_size': 104857600,
  'limits/max_diff_bytes': 1048576, 'limits/max_agent_artifacts': 50, 'security/allow_symlinks': false,
  'serving/keep_versions': 0, 'serving/public_base_url': 'http://localhost:8080', ...over });

export const ADMIN_SUBJECT = 'service:web:user:3';

// An admin route module on a real listener. The server.js guard admits only a viewless
// internal_rest and parses JSON, so a stand-in middleware does what that mount does; set
// `capability` on the handle to try another caller.
export async function startAdminApp(register, cfg, extraCtx = {}) {
  // Loaded here, so a file that needs only the store helpers does not load the route stack.
  const [{ default: express }, { createModuleRouteApp }] = await Promise.all([import('express'), import('../../config-loader.js')]);
  const h = { capability: { kind: 'internal_rest', subject_id: ADMIN_SUBJECT, agent_view_id: null } };
  const app = express();
  app.use((req, _res, next) => { req.capability = h.capability; next(); }, express.json());
  await register({ tool: () => {} }, {
    app: createModuleRouteApp(app, vi.fn()), log: vi.fn(), db: null, moduleConfigs: { versioned_artifacts: cfg }, ...extraCtx,
  });
  const listener = await new Promise((resolve) => { const l = app.listen(0, '127.0.0.1', () => resolve(l)); });
  h.origin = `http://127.0.0.1:${listener.address().port}`;
  h.close = () => new Promise((resolve) => listener.close(resolve));
  return h;
}
