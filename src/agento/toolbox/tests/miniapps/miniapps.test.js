import { describe, it, expect, vi } from 'vitest';
import { createMiniapps, parseManifest, fingerprint, register, MANIFEST_FILE }
  from '../../../modules/miniapps/toolbox/miniapps.js';

const V1 = 'v-20260101-120000-aaaa';
const V2 = 'v-20260102-120000-bbbb';
const manifest = (over = {}) => Buffer.from(JSON.stringify({ schema: 1, title: 'Demo', actions: ['jira_get_issue', 'jira_search'], ...over }));

// A versioned_artifacts service stand-in: files per version, and the scope's artifacts.
function fakeVa(files = {}, current = { site: V1 }) {
  return {
    readVersionFile: vi.fn(async (code, v, name) => (name === MANIFEST_FILE ? files[`${code}@${v}`] ?? null : null)),
    listArtifacts: vi.fn(async () => Object.entries(current).map(([artifact_code, current_version]) => ({ artifact_code, current_version }))),
  };
}
// The activation and audit tables in memory.
function fakeDb() {
  const rows = new Map(); const audit = [];
  const pool = {
    execute: async (sql, p) => {
      if (/INSERT INTO versioned_artifact_audit/.test(sql)) { audit.push(p); return [{}]; }
      if (/INSERT INTO miniapp_activation/.test(sql)) {
        rows.set(`${p[0]}@${p[1]}`, { artifact_code: p[0], version_id: p[1], manifest_fingerprint: p[2], allowed_actions: p[3], activated_by: p[4] });
        return [{ affectedRows: 1 }];
      }
      if (/DELETE FROM miniapp_activation/.test(sql)) return [{ affectedRows: rows.delete(`${p[0]}@${p[1]}`) ? 1 : 0 }];
      throw new Error(`unexpected ${sql}`);
    },
    query: async (sql, p) => {
      if (/WHERE artifact_code = \? AND version_id = \?/.test(sql)) { const r = rows.get(`${p[0]}@${p[1]}`); return [r ? [r] : []]; }
      if (/FROM miniapp_activation ORDER BY/.test(sql)) return [[...rows.values()]];
      throw new Error(`unexpected ${sql}`);
    },
  };
  return { rows, audit, db: { getCronPool: () => pool } };
}
const mk = (va, db, over = {}) => createMiniapps({ service: va, db, vaConfig: { storage_root: '/tmp/x' }, actor: 'op', admin: true, ...over });

describe('manifest', () => {
  it('accepts the strict shape', () => {
    expect(parseManifest(manifest())).toEqual({ title: 'Demo', actions: ['jira_get_issue', 'jira_search'] });
    expect(parseManifest(manifest({ actions: [] }))).toEqual({ title: 'Demo', actions: [] });
  });
  it.each([
    ['an unknown key', { extra: 1 }], ['another schema', { schema: 2 }], ['an empty title', { title: '' }],
    ['a long title', { title: 'x'.repeat(201) }], ['a duplicate action', { actions: ['a', 'a'] }],
    ['a bad action name', { actions: ['Bad-Name'] }], ['too many actions', { actions: Array.from({ length: 65 }, (_, i) => `t${i}`) }],
    ['actions not a list', { actions: 'a' }],
  ])('refuses %s', (_n, over) => expect(parseManifest(manifest(over))).toBeNull());
  it('refuses a missing key, not JSON and an array', () => {
    expect(parseManifest(Buffer.from('{"schema":1,"title":"x"}'))).toBeNull();
    expect(parseManifest(Buffer.from('nope'))).toBeNull();
    expect(parseManifest(Buffer.from('[]'))).toBeNull();
  });
});

describe('activation', () => {
  it('activates with all actions by default, records the fingerprint, and audits', async () => {
    const { rows, audit, db } = fakeDb();
    const r = await mk(fakeVa({ [`site@${V1}`]: manifest() }), db).activate('site', V1);
    expect(r).toEqual({ artifact_code: 'site', version_id: V1, manifest_fingerprint: fingerprint(manifest()),
      allowed_actions: ['jira_get_issue', 'jira_search'] });
    expect(rows.get(`site@${V1}`).manifest_fingerprint).toBe(fingerprint(manifest()));
    expect(audit.at(-1)).toContain('miniapp.activate');
  });

  it('narrows to a subset and refuses an action the manifest does not declare', async () => {
    const { db, audit } = fakeDb();
    const apps = mk(fakeVa({ [`site@${V1}`]: manifest() }), db);
    expect((await apps.activate('site', V1, ['jira_search'])).allowed_actions).toEqual(['jira_search']);
    await expect(apps.activate('site', V1, ['shell_exec'])).rejects.toThrow(/ACTION_NOT_DECLARED/);
    expect(audit.at(-1)).toContain('error');
  });

  it('refuses a version without a valid manifest', async () => {
    const { db } = fakeDb();
    await expect(mk(fakeVa({}), db).activate('site', V1)).rejects.toThrow(/MANIFEST_INVALID/);
    await expect(mk(fakeVa({ [`site@${V1}`]: Buffer.from('{}') }), db).activate('site', V1)).rejects.toThrow(/MANIFEST_INVALID/);
  });

  it('is operator only', async () => {
    const { db } = fakeDb();
    await expect(mk(fakeVa({ [`site@${V1}`]: manifest() }), db, { admin: false }).activate('site', V1))
      .rejects.toThrow(/MINIAPP_ACCESS_DENIED/);
  });

  it('deactivates by deleting the row; a second deactivate is NOT_ACTIVATED', async () => {
    const { db, rows } = fakeDb();
    const apps = mk(fakeVa({ [`site@${V1}`]: manifest() }), db);
    await apps.activate('site', V1);
    await apps.deactivate('site', V1);
    expect(rows.size).toBe(0);
    await expect(apps.deactivate('site', V1)).rejects.toThrow(/NOT_ACTIVATED/);
  });
});

// The three states stay separate (PRD E6 §3).
describe('launch spec and catalogue', () => {
  it('saved but not activated launches files-only', async () => {
    const { db } = fakeDb();
    expect(await mk(fakeVa({ [`site@${V1}`]: manifest() }), db).launchSpec('site', V1)).toEqual({ activated: false });
  });

  it('activated gives the pinned fingerprint and actions; another version of it is not activated', async () => {
    const { db } = fakeDb();
    const apps = mk(fakeVa({ [`site@${V1}`]: manifest(), [`site@${V2}`]: manifest() }), db);
    await apps.activate('site', V1, ['jira_search']);
    expect(await apps.launchSpec('site', V1)).toEqual({ activated: true, manifest_fingerprint: fingerprint(manifest()),
      allowed_actions: ['jira_search'] });
    expect(await apps.launchSpec('site', V2)).toEqual({ activated: false });
  });

  it('a manifest that no longer matches the activation is not activated', async () => {
    const { db } = fakeDb();
    const files = { [`site@${V1}`]: manifest() };
    const apps = mk(fakeVa(files), db);
    await apps.activate('site', V1);
    files[`site@${V1}`] = manifest({ title: 'Changed' });
    expect(await apps.launchSpec('site', V1)).toEqual({ activated: false });
  });

  it('the catalogue lists only an activated current version', async () => {
    const { db } = fakeDb();
    const files = { [`site@${V1}`]: manifest(), [`other@${V2}`]: manifest({ title: 'Other' }) };
    const apps = mk(fakeVa(files, { site: V1, other: V2, plain: V1 }), db);
    await apps.activate('site', V1);
    expect(await apps.catalogue()).toEqual([{ artifact_code: 'site', version_id: V1, title: 'Demo' }]);
  });
});

describe('tools', () => {
  it('registers each tool only when its own key is enabled', async () => {
    const names = [];
    const server = { tool: (name) => names.push(name) };
    const ctx = (enabled) => ({ log: vi.fn(), moduleConfigs: { versioned_artifacts: {} }, db: null, agentViewId: 3,
      isToolEnabled: (n) => enabled.includes(n) });
    // A VA config the service refuses registers nothing.
    await register(server, ctx(['miniapp_get_launch_spec', 'miniapp_list']));
    expect(names).toEqual([]);
    const good = { storage_root: '/srv/s', published_root: '/srv/p', 'serving/keep_versions': 0,
      'limits/max_file_size': 1, 'limits/max_total_size': 1, 'limits/max_files': 1, 'limits/max_diff_bytes': 1,
      'limits/max_agent_artifacts': 1, 'security/allow_symlinks': false };
    await register(server, { ...ctx(['miniapp_list']), moduleConfigs: { versioned_artifacts: good } });
    expect(names).toEqual(['miniapp_list']);
  });
});
