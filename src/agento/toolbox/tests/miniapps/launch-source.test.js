import { describe, it, expect } from 'vitest';
import { authSources, checkLaunch } from '../../../modules/miniapps/toolbox/launch-source.js';
import { GRANTS_SQL, grantsFor } from '../../capability.js';

const LAUNCH = { id: 'L1', user_id: 7, role: 'user', artifact_code: 'site', version_id: 'v-20260101-120000-aaaa',
  workspace_id: 1, agent_view_id: 11, created_at: 1000, expires_at: 2000, allowed_actions: '["jira_search"]' };

// The checker's SQL over in-memory state. The launch query carries every liveness and
// approval condition, so "no row" stands for each of them (revoked, expired, not
// redeemed, inactive user, revoked approval, another approved version).
function fakeQuery({ launch = LAUNCH, ops = ['artifact.launch'] } = {}) {
  return async (sql, params) => {
    if (sql.includes('FROM agent_view')) return [params[0] === 11 ? [{ workspace_id: 1 }] : []];
    if (sql.includes('FROM launch l')) {
      expect(sql).toMatch(/exchange_redeemed_at IS NOT NULL/);
      expect(sql).toMatch(/revoked_at IS NULL/);
      expect(sql).toMatch(/expires_at > NOW\(\)/);
      expect(sql).toMatch(/is_active = 1/);
      expect(sql).toMatch(/a\.agent_view_id = l\.agent_view_id AND a\.approved_version_id = l\.version_id/);
      return [launch && params[0] === launch.id ? [launch] : []];
    }
    if (sql === GRANTS_SQL) {
      // O-2: the approval is the grant; the role's tool grants are never read.
      expect(params[1]).toBe('operation');
      return [ops.map(name => ({ name }))];
    }
    throw new Error(`unexpected SQL: ${sql}`);
  };
}
// The verifier hands a checker the framework's grant rule for the row's own scope.
const opts = (over = {}) => {
  const o = { capability_kind: 'miniapp', workspace_id: 1, agent_view_id: 11, query: fakeQuery(), ...over };
  return { grants: grantsFor(o.query, o.workspace_id, o.agent_view_id), ...o };
};

describe('launch auth source', () => {
  it('exports the launch kind', () => expect(authSources).toEqual([['launch', checkLaunch]]));

  it('returns the launch, its scope and the approved actions', async () => {
    expect(await checkLaunch('L1', opts())).toEqual({
      kind: 'launch', id: 'L1', user_id: '7', workspace_id: 1, agent_view_id: 11, permitted_tools: ['jira_search'],
      created_at: 1000, expires_at: 2000, launch_id: 'L1', artifact_code: 'site', version_id: 'v-20260101-120000-aaaa',
    });
  });

  it('refuses a launch the query no longer finds', async () => {
    expect(await checkLaunch('L1', opts({ query: fakeQuery({ launch: null }) }))).toBeNull();
    expect(await checkLaunch('L2', opts())).toBeNull();
  });

  it('refuses when the verifier hands no grant rule (fails closed)', async () => {
    expect(await checkLaunch('L1', { ...opts(), grants: undefined })).toBeNull();
  });

  it('reads the approved actions as a driver may return them: JSON text or a parsed list', async () => {
    const parsed = fakeQuery({ launch: { ...LAUNCH, allowed_actions: ['jira_search'] } });
    expect((await checkLaunch('L1', opts({ query: parsed }))).permitted_tools).toEqual(['jira_search']);
  });

  it('refuses when artifact.launch was removed or the approval allows no action', async () => {
    expect(await checkLaunch('L1', opts({ query: fakeQuery({ ops: [] }) }))).toBeNull();
    expect(await checkLaunch('L1', opts({ query: fakeQuery({ launch: { ...LAUNCH, allowed_actions: '[]' } }) })))
      .toBeNull();
  });

  it('refuses a launch with no agent view (an approval is per view)', async () => {
    expect(await checkLaunch('L1', opts({ agent_view_id: null }))).toBeNull();
  });

  it('refuses another kind, a scope the launch does not have, and a view of another workspace', async () => {
    expect(await checkLaunch('L1', opts({ capability_kind: 'user_session' }))).toBeNull();
    expect(await checkLaunch('L1', opts({ query: fakeQuery({ launch: { ...LAUNCH, agent_view_id: 12 } }) }))).toBeNull();
    expect(await checkLaunch('L1', opts({ agent_view_id: 22 }))).toBeNull();
    expect(await checkLaunch('L1', opts({ workspace_id: 0 }))).toBeNull();
  });
});

// The prune keeps each approved version's tree while miniapps is enabled (plan §2.4).
describe('retention pin', () => {
  it('pins the approved version of each activation row of the artifact', async () => {
    const { retentionPins } = await import('../../../modules/miniapps/toolbox/launch-source.js');
    expect(retentionPins).toEqual(
      ['SELECT approved_version_id AS version_id FROM miniapp_activation WHERE artifact_code = ?']);
  });
});
