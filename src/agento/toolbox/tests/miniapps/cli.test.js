import { it, expect } from 'vitest';
import { main } from '../../../modules/miniapps/toolbox/cli.js';

const VA = {
  storage_root: '/nonexistent/va', published_root: '/nonexistent/pub', allowed_artifacts: '',
  'serving/keep_versions': 0, 'serving/public_base_url': 'http://localhost:8080',
  'limits/max_files': 2000, 'limits/max_file_size': 5242880,
  'limits/max_total_size': 104857600, 'limits/max_diff_bytes': 1048576,
  'limits/max_agent_artifacts': 50, 'security/allow_symlinks': false,
};
const deps = (configs = { versioned_artifacts: VA }, db = null) => ({
  loadModuleConfigs: async () => configs,
  db,
});
const PAYLOAD = { artifact_code: 'site', version_id: 'v-20260101-120000-aaaa' };

it('answers an unknown operation with one error line, not an exception', async () => {
  expect(await main({ op: 'drop' }, {}, deps())).toEqual({ error_code: 'INVALID_OPERATION', message: 'unknown operation' });
});

it('refuses to run without the versioned_artifacts config', async () => {
  await expect(main({ op: 'list' }, {}, deps({}))).rejects.toThrow(/versioned_artifacts config unavailable/);
});

it.each(['list', 'deactivate'])('%s without the activation store is MINIAPP_STORE_UNAVAILABLE', async (op) => {
  const r = await main({ op }, PAYLOAD, deps());
  expect(r.error_code).toBe('MINIAPP_STORE_UNAVAILABLE');
});

it('rejects a bad artifact code before it touches the store', async () => {
  const r = await main({ op: 'deactivate' }, { artifact_code: 'Bad!', version_id: PAYLOAD.version_id }, deps());
  expect(r.error_code).toBe('INVALID_PATH');
});
