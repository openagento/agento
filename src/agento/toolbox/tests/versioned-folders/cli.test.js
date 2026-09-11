import { it, expect, beforeEach, afterEach } from 'vitest';
import { fileURLToPath } from 'node:url';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { main } from '../../../modules/versioned_folders/toolbox/cli.js';

let root;
const CONFIG = () => ({
  storage_root: root, allowed_folders: '',
  'limits/max_files': 2000, 'limits/max_file_size': 5242880,
  'limits/max_total_size': 104857600, 'limits/max_diff_bytes': 1048576,
  'security/allow_symlinks': false,
});
const deps = (config = CONFIG()) => ({
  loadModuleConfigs: async () => (config ? { versioned_folders: config } : {}),
  db: null,
});
const b64 = (s) => Buffer.from(s, 'utf8').toString('base64');
const FILES = [{ path: 'index.html', content: b64('<h1>hi</h1>'), encoding: 'base64' }];

beforeEach(async () => { root = await mkdtemp(path.join(tmpdir(), 'vf-cli-')); });
afterEach(async () => { await rm(root, { recursive: true, force: true }); });

it('creates a folder from an inline files[] payload', async () => {
  const r = await main({ actor: 'admin' }, { folder_code: 'site', files: FILES }, deps());
  expect(r.current_version).toMatch(/^v-\d{8}-\d{6}-[a-z0-9]{4}$/);
  // init is exempt from the allowlist — an administrator creates the folder
  // before any scope could name it.
  expect(r.folder_code).toBe('site');
});

it('refuses a second init of the same folder code', async () => {
  await main({ actor: 'admin' }, { folder_code: 'site', files: FILES }, deps());
  const r = await main({ actor: 'admin' }, { folder_code: 'site', files: FILES }, deps());
  expect(r.error_code).toBe('GIT_OPERATION_FAILED');
  expect(r.message).toMatch(/already exists/);
});

it('rejects a payload path containing ..', async () => {
  const r = await main({ actor: 'admin' },
    { folder_code: 'site', files: [{ path: '../escape.txt', content: b64('x'), encoding: 'base64' }] }, deps());
  expect(r.error_code).toBe('INVALID_PATH');
});

it('rejects a symlink entry', async () => {
  const r = await main({ actor: 'admin' },
    { folder_code: 'site', files: [{ path: 'link.txt', symlink: true }] }, deps());
  expect(r.error_code).toBe('SYMLINK_NOT_ALLOWED');
});

it('returns an error_code rather than throwing a host stack trace', async () => {
  const r = await main({ actor: 'admin' },
    { folder_code: 'NOT-A-CODE', files: FILES }, deps());
  expect(r.error_code).toBe('INVALID_PATH');
  expect(JSON.stringify(r)).not.toMatch(/at Object\.|node:internal|fatal:/);
});

it('resolves its storage root and limits from the injected config, not a default', async () => {
  // A hardcoded /srv/versioned-folders would write outside the injected root and
  // this assertion would find nothing there.
  await main({ actor: 'admin' }, { folder_code: 'site', files: FILES }, deps());
  await expect(readFile(path.join(root, 'site', 'repo.git', 'HEAD'), 'utf8')).resolves.toContain('ref:');
  const tight = await main({ actor: 'admin' },
    { folder_code: 'other', files: FILES }, deps({ ...CONFIG(), 'limits/max_file_size': 2 }));
  expect(tight.error_code).toBe('FILE_TOO_LARGE');
});

it('fails loudly when the module config is unavailable', async () => {
  // Not an operation result: the CLI cannot run at all, so this REJECTS rather
  // than resolving to an error_code the wrapper would print as a normal failure.
  await expect(main({ actor: 'admin' }, { folder_code: 'site', files: FILES }, deps(null)))
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
  const cliPath = fileURLToPath(new URL('../../../modules/versioned_folders/toolbox/cli.js', import.meta.url));
  const env = { ...process.env };
  delete env.NODE_OPTIONS;
  const { stdout, stderr, code } = await new Promise((resolve) => {
    const child = execFile(process.execPath, [cliPath], { env },
      (err, so, se) => resolve({ stdout: so, stderr: se, code: err?.code ?? 0 }));
    child.stdin.end('{}');
  });
  expect(code).toBe(1);
  expect(JSON.parse(stdout.trim())).toEqual({ error_code: 'GIT_OPERATION_FAILED', message: 'the toolbox command failed' });
  expect(stderr).not.toMatch(/node:internal|at |file:\/\/|\^/);
});
