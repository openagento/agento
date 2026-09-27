import { describe, it, expect } from 'vitest';
import { authSources, checkLaunch } from '../../../modules/miniapps/toolbox/launch-source.js';
import { GRANTS_SQL } from '../../../modules/web/toolbox/auth-sources.js';

const LAUNCH = { id: 'L1', user_id: 7, role: 'user', artifact_code: 'site', version_id: 'v-20260101-120000-aaaa',
  workspace_id: 1, agent_view_id: 11, created_at: 1000, expires_at: 2000 };

// The checker's SQL over in-memory state. The launch query carries every liveness and
// activation condition, so "no row" stands for each of them (revoked, expired, not
// redeemed, inactive user, deactivated, other fingerprint).
function fakeQuery({ launch = LAUNCH, ops = ['artifact.launch'], tools = ['jira_search'] } = {}) {
  return async (sql, params) => {
    if (sql.includes('FROM agent_view')) return [params[0] === 11 ? [{ workspace_id: 1 }] : []];
    if (sql.includes('FROM launch l')) {
      expect(sql).toMatch(/exchange_redeemed_at IS NOT NULL/);
      expect(sql).toMatch(/revoked_at IS NULL/);
      expect(sql).toMatch(/expires_at > NOW\(\)/);
      expect(sql).toMatch(/is_active = 1/);
      expect(sql).toMatch(/a\.manifest_fingerprint = l\.manifest_fingerprint/);
      return [launch && params[0] === launch.id ? [launch] : []];
    }
    if (sql === GRANTS_SQL) return [(params[1] === 'operation' ? ops : tools).map(name => ({ name }))];
    throw new Error(`unexpected SQL: ${sql}`);
  };
}
const opts = (over = {}) => ({ capability_kind: 'miniapp', workspace_id: 1, agent_view_id: 11, query: fakeQuery(), ...over });

describe('launch auth source', () => {
  it('exports the launch kind', () => expect(authSources).toEqual([['launch', checkLaunch]]));

  it('returns the launch, its scope and the grants for it', async () => {
    expect(await checkLaunch('L1', opts())).toEqual({
      kind: 'launch', id: 'L1', user_id: '7', workspace_id: 1, agent_view_id: 11, permitted_tools: ['jira_search'],
      created_at: 1000, expires_at: 2000, launch_id: 'L1', artifact_code: 'site', version_id: 'v-20260101-120000-aaaa',
    });
  });

  it('refuses a launch the query no longer finds', async () => {
    expect(await checkLaunch('L1', opts({ query: fakeQuery({ launch: null }) }))).toBeNull();
    expect(await checkLaunch('L2', opts())).toBeNull();
  });

  it('refuses when artifact.launch or every tool grant was removed', async () => {
    expect(await checkLaunch('L1', opts({ query: fakeQuery({ ops: [] }) }))).toBeNull();
    expect(await checkLaunch('L1', opts({ query: fakeQuery({ tools: [] }) }))).toBeNull();
  });

  it('refuses another kind, a scope the launch does not have, and a view of another workspace', async () => {
    expect(await checkLaunch('L1', opts({ capability_kind: 'user_session' }))).toBeNull();
    expect(await checkLaunch('L1', opts({ query: fakeQuery({ launch: { ...LAUNCH, agent_view_id: 12 } }) }))).toBeNull();
    expect(await checkLaunch('L1', opts({ agent_view_id: 22 }))).toBeNull();
    expect(await checkLaunch('L1', opts({ workspace_id: 0 }))).toBeNull();
  });
});
