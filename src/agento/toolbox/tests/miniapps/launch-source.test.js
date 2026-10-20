import { describe, it, expect } from 'vitest';
import { authSources, checkLaunch } from '../../../modules/miniapps/toolbox/launch-source.js';
import { GRANTS_SQL, grantsFor } from '../../capability.js';

const LAUNCH = { id: 'L1', user_id: 7, role: 'user', artifact_code: 'site', version_id: 'v-20260101-120000-aaaa',
  workspace_id: 1, agent_view_id: 11, created_at: 1000, expires_at: 2000, allowed_actions: '["jira_search"]',
  manifest_actions: '["jira_search","jira_add_comment"]' };

// The checker's SQL over in-memory state. The launch query carries every liveness, approval
// and `view` condition, so "no row" stands for each of them (revoked, expired, not redeemed,
// inactive user, revoked approval, another approved version, no member row and not creator).
function fakeQuery({ launch = LAUNCH, ops = ['artifact.launch'], tools = ['jira_search', 'jira_add_comment'] } = {}) {
  return async (sql, params) => {
    if (sql.includes('FROM agent_view')) return [params[0] === 11 ? [{ workspace_id: 1 }] : []];
    if (sql.includes('FROM launch l')) return [launch && params[0] === launch.id ? [launch] : []];
    if (sql === GRANTS_SQL) return [(params[1] === 'operation' ? ops : tools).map(name => ({ name }))];
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

  it('puts every liveness, approval and view condition in the launch query', async () => {
    const seen = [];
    const query = fakeQuery();
    await checkLaunch('L1', opts({ query: async (sql, params) => { seen.push(sql); return query(sql, params); } }));
    const sql = seen.find(q => q.includes('FROM launch l'));
    expect(sql).toMatch(/exchange_redeemed_at IS NOT NULL/);
    expect(sql).toMatch(/revoked_at IS NULL/);
    expect(sql).toMatch(/expires_at > NOW\(\)/);
    expect(sql).toMatch(/is_active = 1/);
    expect(sql).toMatch(/a\.agent_view_id = l\.agent_view_id AND a\.approved_version_id = l\.version_id/);
    expect(sql).toMatch(/u\.role = 'admin' OR EXISTS \(SELECT 1 FROM miniapp_member m/);
    expect(sql).toMatch(/j\.requester_trust = 'account'/);
  });

  it.each([
    ['a launch the query no longer finds (revoked, expired, member row gone, ...)', 'L1', () => opts({ query: fakeQuery({ launch: null }) })],
    ['an unknown launch id', 'L2', () => opts()],
    ['when the verifier hands no grant rule (fails closed)', 'L1', () => ({ ...opts(), grants: undefined })],
    ['when artifact.launch was removed', 'L1', () => opts({ query: fakeQuery({ ops: [] }) })],
    ['when the approval allows no action', 'L1', () => opts({ query: fakeQuery({ launch: { ...LAUNCH, allowed_actions: '[]' } }) })],
    ['when one manifest action has no tool grant, even if the approved subset is held (S5)', 'L1',
      () => opts({ query: fakeQuery({ tools: ['jira_search'] }) })],
    ['an approval made before the manifest was stored (NULL): fail closed', 'L1',
      () => opts({ query: fakeQuery({ launch: { ...LAUNCH, manifest_actions: null } }) })],
    ['a launch with no agent view (an approval is per view)', 'L1', () => opts({ agent_view_id: null })],
    ['another kind', 'L1', () => opts({ capability_kind: 'user_session' })],
    ['a scope the launch does not have', 'L1', () => opts({ query: fakeQuery({ launch: { ...LAUNCH, agent_view_id: 12 } }) })],
    ['a view of another workspace', 'L1', () => opts({ agent_view_id: 22 })],
    ['another workspace', 'L1', () => opts({ workspace_id: 0 })],
  ])('refuses %s', async (_name, id, o) => {
    expect(await checkLaunch(id, o())).toBeNull();
  });

  it('reads the approved actions as a driver may return them: JSON text or a parsed list', async () => {
    const parsed = fakeQuery({ launch: { ...LAUNCH, allowed_actions: ['jira_search'] } });
    expect((await checkLaunch('L1', opts({ query: parsed }))).permitted_tools).toEqual(['jira_search']);
  });

  it('allows the creator or a member with the grants (the query found the row)', async () => {
    const parsed = fakeQuery({ launch: { ...LAUNCH, manifest_actions: ['jira_search', 'jira_add_comment'] } });
    expect((await checkLaunch('L1', opts({ query: parsed }))).permitted_tools).toEqual(['jira_search']);
  });

  it('allows admin with no grant at all (S4); the ceiling stays the approved actions', async () => {
    const admin = fakeQuery({ launch: { ...LAUNCH, role: 'admin', manifest_actions: null }, ops: [], tools: [] });
    expect((await checkLaunch('L1', opts({ query: admin }))).permitted_tools).toEqual(['jira_search']);
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
