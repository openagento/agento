import { it, expect, beforeEach, afterEach } from 'vitest';
import { fileURLToPath } from 'node:url';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { main } from '../../../modules/versioned_artifacts/toolbox/cli.js';
import { createBackend } from '../../../modules/versioned_artifacts/toolbox/git-backend.js';
import { mintVersion } from './helpers.js';

let root, pub;
const CONFIG = () => ({
  storage_root: root, published_root: pub, allowed_artifacts: '',
  'serving/keep_versions': 0, 'serving/public_base_url': 'http://localhost:8080',
  'limits/max_files': 2000, 'limits/max_file_size': 5242880,
  'limits/max_total_size': 104857600, 'limits/max_diff_bytes': 1048576,
  'limits/max_agent_artifacts': 50,
  'security/allow_symlinks': false,
});
const deps = (config = CONFIG()) => ({
  loadModuleConfigs: async () => (config ? { versioned_artifacts: config } : {}),
  db: null,
});
const b64 = (s) => Buffer.from(s, 'utf8').toString('base64');
const FILES = [{ path: 'index.html', content: b64('<h1>hi</h1>'), encoding: 'base64' }];

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'vf-cli-'));
  pub = await mkdtemp(path.join(tmpdir(), 'vf-cli-pub-'));
});
afterEach(async () => {
  await rm(root, { recursive: true, force: true });
  await rm(pub, { recursive: true, force: true });
});

it('creates an artifact from an inline files[] payload', async () => {
  const r = await main({ actor: 'admin' }, { artifact_code: 'site', files: FILES }, deps());
  expect(r.current_version).toMatch(/^v-\d{8}-\d{6}-[a-z0-9]{4}$/);
  // init is exempt from the allowlist — an administrator creates the artifact
  // before any scope could name it.
  expect(r.artifact_code).toBe('site');
});

it('refuses a second init of the same artifact code', async () => {
  // The OPERATOR path never gets the `-N` an agent gets: `artifact:init` names a code
  // deliberately, and publishing at a different address than the one asked for would
  // answer a request nobody made.
  await main({ actor: 'admin' }, { artifact_code: 'site', files: FILES }, deps());
  const r = await main({ actor: 'admin' }, { artifact_code: 'site', files: FILES }, deps());
  expect(r.error_code).toBe('ARTIFACT_ALREADY_EXISTS');
  expect(r.message).toMatch(/already exists/);
});

it('rejects a payload path containing ..', async () => {
  const r = await main({ actor: 'admin' },
    { artifact_code: 'site', files: [{ path: '../escape.txt', content: b64('x'), encoding: 'base64' }] }, deps());
  expect(r.error_code).toBe('INVALID_PATH');
});

it('rejects a symlink entry', async () => {
  const r = await main({ actor: 'admin' },
    { artifact_code: 'site', files: [{ path: 'link.txt', symlink: true }] }, deps());
  expect(r.error_code).toBe('SYMLINK_NOT_ALLOWED');
});

it('returns an error_code rather than throwing a host stack trace', async () => {
  const r = await main({ actor: 'admin' },
    { artifact_code: 'NOT-A-CODE', files: FILES }, deps());
  expect(r.error_code).toBe('INVALID_PATH');
  expect(JSON.stringify(r)).not.toMatch(/at Object\.|node:internal|fatal:/);
});

it('resolves its storage root and limits from the injected config, not a default', async () => {
  // A hardcoded /srv/versioned-artifacts would write outside the injected root and
  // this assertion would find nothing there.
  await main({ actor: 'admin' }, { artifact_code: 'site', files: FILES }, deps());
  await expect(readFile(path.join(root, 'site', 'repo.git', 'HEAD'), 'utf8')).resolves.toContain('ref:');
  const tight = await main({ actor: 'admin' },
    { artifact_code: 'other', files: FILES }, deps({ ...CONFIG(), 'limits/max_file_size': 2 }));
  expect(tight.error_code).toBe('FILE_TOO_LARGE');
});

it('fails loudly when the module config is unavailable', async () => {
  // Not an operation result: the CLI cannot run at all, so this REJECTS rather
  // than resolving to an error_code the wrapper would print as a normal failure.
  await expect(main({ actor: 'admin' }, { artifact_code: 'site', files: FILES }, deps(null)))
    .rejects.toThrow(/module config unavailable/);
});

it('prints the CONFIGURED limits, which is how the host pre-flight learns them', async () => {
  // The host CLI cannot resolve module config (the fallback reads the DB on the
  // container network), so hardcoding limits there is what this mode replaces.
  const r = await main({ printLimits: true }, {},
    deps({ ...CONFIG(), 'limits/max_file_size': '20971520', 'limits/max_files': '9',
      'limits/max_total_size': '33' }));
  expect(r).toEqual({ max_file_size: 20971520, max_files: 9, max_total_size: 33 });
});

// `allowed_artifacts` is empty in CONFIG above — agent_view-scoped and denying
// everything, which is what this CLI resolves at DEFAULT scope. The admin operations
// must work anyway, or `artifact:list` prints nothing and `artifact:publish` is denied
// on every deployment; the agent path, which is `register()`'s, is unchanged.
it('lists the artifacts the store holds, past the empty default allowlist', async () => {
  await main({ actor: 'admin' }, { artifact_code: 'site', files: FILES }, deps());
  await main({ actor: 'admin' }, { artifact_code: 'docs', files: FILES }, deps());
  const r = await main({ actor: 'admin', op: 'list' }, {}, deps());
  expect(r.artifacts.map((a) => a.artifact_code).sort()).toEqual(['docs', 'site']);
  expect(r.artifacts[0].preview_url).toMatch(/^http:\/\/localhost:8080\//);
});

it('publishes through the same CAS, and refuses a stale --expected', async () => {
  const created = await main({ actor: 'admin' }, { artifact_code: 'site', files: FILES }, deps());
  const v1 = created.current_version;
  const v2 = await mintVersion(createBackend(), root, 'site', '<h1>v2</h1>');
  const ok = await main({ actor: 'admin', op: 'publish' },
    { artifact_code: 'site', version_id: v2, expected_current_version: v1 }, deps());
  expect(ok.current_version).toBe(v2);
  expect(ok.preview_url).toBe('http://localhost:8080/site/');
  // The operator's --expected is now a version behind: the CAS must refuse, not
  // republish. This is the guard the repair instruction depends on.
  const stale = await main({ actor: 'admin', op: 'publish' },
    { artifact_code: 'site', version_id: v1, expected_current_version: v1 }, deps());
  expect(stale.error_code).toBe('CURRENT_VERSION_CHANGED');
  // A version id that names nothing is refused before the CAS is consulted.
  const missing = await main({ actor: 'admin', op: 'publish' },
    { artifact_code: 'site', version_id: 'v-20200101-000000-aaaa', expected_current_version: v2 }, deps());
  expect(missing.error_code).toBe('VERSION_NOT_FOUND');
});

it('answers an unknown --op with an error code rather than a throw', async () => {
  const r = await main({ actor: 'admin', op: 'delete-everything' }, {}, deps());
  expect(r.error_code).toBe('INVALID_OPERATION');
  expect(JSON.stringify(r)).not.toMatch(/at Object\.|node:internal/);
});

// The direct-execution branch, run as a real child process — the only way to reach
// it, since it is guarded on `process.argv[1]` and never runs under Vitest. On the
// host the container-only dynamic imports cannot resolve, which is exactly the
// failure that used to print a Node ESM stack (`node:internal/modules/esm/resolve`)
// where the documented contract says a caller only ever sees one error line.
it('never writes a stack when the direct executor fails (round 3)', async () => {
  const { execFile } = await import('node:child_process');
  // Resolved from THIS FILE, not from the process CWD: `bin/test` runs vitest from
  // src/agento/toolbox while `vitest --root src/agento/toolbox` runs it from the repo
  // root, and a CWD-relative path silently spawns a node that cannot find the module —
  // an empty stdout that reads as "the contract is broken".
  const cliPath = fileURLToPath(new URL('../../../modules/versioned_artifacts/toolbox/cli.js', import.meta.url));
  const env = { ...process.env };
  delete env.NODE_OPTIONS;
  const { stdout, stderr, code } = await new Promise((resolve) => {
    const child = execFile(process.execPath, [cliPath], { env },
      (err, so, se) => resolve({ stdout: so, stderr: se, code: err?.code ?? 0 }));
    child.stdin.end('{}');
  });
  expect(code).toBe(1);
  expect(JSON.parse(stdout.trim())).toEqual({ error_code: 'STORAGE_OPERATION_FAILED', message: 'the toolbox command failed' });
  expect(stderr).not.toMatch(/node:internal|at |file:\/\/|\^/);
});
