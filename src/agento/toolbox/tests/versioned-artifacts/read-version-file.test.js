import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createService } from '../../../modules/versioned_artifacts/toolbox/service.js';

// `readVersionFile`: one root file of a version, as bytes (miniapps reads its manifest
// through it, PRD E6 §4, so no module outside service.js reaches the storage engine).
let root; let pub;
const b64 = (s) => Buffer.from(s).toString('base64');
const files = [
  { path: 'index.html', content: b64('<h1>x</h1>'), encoding: 'base64' },
  { path: 'miniapp.json', content: b64('{"schema":1}'), encoding: 'base64' },
  { path: 'sub/inner.json', content: b64('{}'), encoding: 'base64' },
];
const svc = (over = {}) => createService({ config: { storage_root: root, published_root: pub, allowed_artifacts: 'site',
  'serving/keep_versions': 0, 'serving/public_base_url': 'http://localhost:8080', 'limits/max_files': 2000,
  'limits/max_file_size': 5242880, 'limits/max_total_size': 104857600, 'limits/max_diff_bytes': 1048576,
  'limits/max_agent_artifacts': 50, 'security/allow_symlinks': false, ...over }, db: null, log: vi.fn(), actor: 'a@b.c' });

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'va-rvf-'));
  pub = await mkdtemp(path.join(tmpdir(), 'va-rvf-pub-'));
});
afterEach(async () => {
  await rm(root, { recursive: true, force: true });
  await rm(pub, { recursive: true, force: true });
});

describe('readVersionFile', () => {
  it('reads a root file, and answers null for a missing file or a directory', async () => {
    const { current_version: v } = await svc().init('site', { files });
    expect((await svc().readVersionFile('site', v, 'miniapp.json')).toString()).toBe('{"schema":1}');
    expect(await svc().readVersionFile('site', v, 'absent.json')).toBeNull();
    expect(await svc().readVersionFile('site', v, 'sub')).toBeNull();
  });

  it('refuses a path, a dot name, an oversized file, an unknown version and a code the scope may not use', async () => {
    const { current_version: v } = await svc().init('site', { files });
    await expect(svc().readVersionFile('site', v, 'sub/inner.json')).rejects.toThrow(/INVALID_PATH/);
    await expect(svc().readVersionFile('site', v, '.auth')).rejects.toThrow(/INVALID_PATH/);
    await expect(svc().readVersionFile('site', v, 'miniapp.json', 4)).rejects.toThrow(/FILE_TOO_LARGE/);
    await expect(svc().readVersionFile('site', 'v-20200101-000000-aaaa', 'miniapp.json')).rejects.toThrow(/VERSION_NOT_FOUND/);
    await expect(svc({ allowed_artifacts: '' }).readVersionFile('site', v, 'miniapp.json')).rejects.toThrow(/ACCESS_DENIED/);
  });
});
