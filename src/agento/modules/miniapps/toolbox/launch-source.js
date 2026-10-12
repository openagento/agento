// The `launch` auth source (PRD E6 §10, E1 §3.2): on every `miniapp` call the toolbox
// re-checks that the launch behind the capability is still redeemed and live, that its
// user is active and still holds `artifact.launch` in the launch's view, and that the
// launch's version is still the one approved for that view. The approval's actions are
// what the capability's ceiling may reach: the approval is the grant (D-MINIAPP-2).

// Fixture tests/fixtures/miniapp_sql_v1.json; the integration test runs this checker for real.
export const LAUNCH_SQL =
  'SELECT l.id, l.user_id, u.role, l.artifact_code, l.version_id, l.workspace_id, l.agent_view_id, ' +
  'UNIX_TIMESTAMP(l.created_at) AS created_at, UNIX_TIMESTAMP(l.expires_at) AS expires_at, a.allowed_actions ' +
  'FROM launch l JOIN `user` u ON u.id = l.user_id ' +
  'JOIN miniapp_activation a ON a.artifact_code = l.artifact_code ' +
  'AND a.agent_view_id = l.agent_view_id AND a.approved_version_id = l.version_id ' +
  'WHERE l.id = ? AND l.exchange_redeemed_at IS NOT NULL AND l.revoked_at IS NULL ' +
  'AND l.expires_at > NOW() AND u.is_active = 1';

const VIEW_WORKSPACE_SQL = 'SELECT workspace_id FROM agent_view WHERE id = ?';

const isPositiveInt = v => Number.isInteger(v) && v > 0;

// `grants(role, kind)` is the framework's grant rule for the capability's own scope.
export async function checkLaunch(sourceId, { capability_kind, workspace_id, agent_view_id, query, grants } = {}) {
  if (capability_kind !== 'miniapp' || typeof sourceId !== 'string' || typeof query !== 'function'
      || typeof grants !== 'function') return null;
  // An approval is per view: a capability with no view has no launch to act for.
  if (!isPositiveInt(workspace_id) || !isPositiveInt(agent_view_id)) return null;
  const [views] = await query(VIEW_WORKSPACE_SQL, [agent_view_id]);
  if (views.length !== 1 || Number(views[0].workspace_id) !== workspace_id) return null;
  const [launches] = await query(LAUNCH_SQL, [sourceId]);
  if (launches.length !== 1) return null;
  const l = launches[0];
  if (Number(l.workspace_id) !== workspace_id || Number(l.agent_view_id) !== agent_view_id) return null;
  if (!(await grants(l.role, 'operation')).includes('artifact.launch')) return null;
  // mysql2 may hand the JSON column back parsed or as text.
  const tools = Array.isArray(l.allowed_actions) ? l.allowed_actions : JSON.parse(String(l.allowed_actions));
  if (!Array.isArray(tools) || tools.length === 0) return null;
  return {
    kind: 'launch',
    id: l.id,
    user_id: String(l.user_id),
    workspace_id,
    agent_view_id,
    permitted_tools: tools,
    created_at: Number(l.created_at),
    expires_at: Number(l.expires_at),
    launch_id: l.id,
    artifact_code: l.artifact_code,
    version_id: l.version_id,
  };
}

export const authSources = [['launch', checkLaunch]];

// The versioned_artifacts prune keeps every approved version's served tree (config-loader
// collects this).
export const retentionPins = [
  'SELECT approved_version_id AS version_id FROM miniapp_activation WHERE artifact_code = ?',
];
