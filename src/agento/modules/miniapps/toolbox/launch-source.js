// The `launch` auth source (PRD E6 §10, E1 §3.2): on every `miniapp` call the toolbox
// re-checks that the launch behind the capability is still redeemed and live, that its
// user is active and still holds `artifact.launch` in the launch's scope, and that the
// version is still activated with the manifest the launch pinned. The user's tool grants
// for that scope bound what the capability's ceiling may reach.
import { GRANTS_SQL } from '../../web/toolbox/auth-sources.js';

// Fixture tests/fixtures/miniapp_sql_v1.json; the integration test runs this checker for real.
export const LAUNCH_SQL =
  'SELECT l.id, l.user_id, u.role, l.artifact_code, l.version_id, l.workspace_id, l.agent_view_id, ' +
  'UNIX_TIMESTAMP(l.created_at) AS created_at, UNIX_TIMESTAMP(l.expires_at) AS expires_at ' +
  'FROM launch l JOIN `user` u ON u.id = l.user_id ' +
  'JOIN miniapp_activation a ON a.artifact_code = l.artifact_code AND a.version_id = l.version_id ' +
  'AND a.manifest_fingerprint = l.manifest_fingerprint ' +
  'WHERE l.id = ? AND l.exchange_redeemed_at IS NOT NULL AND l.revoked_at IS NULL ' +
  'AND l.expires_at > NOW() AND u.is_active = 1';

const VIEW_WORKSPACE_SQL = 'SELECT workspace_id FROM agent_view WHERE id = ?';

const isPositiveInt = v => Number.isInteger(v) && v > 0;

export async function checkLaunch(sourceId, { capability_kind, workspace_id, agent_view_id, query } = {}) {
  if (capability_kind !== 'miniapp' || typeof sourceId !== 'string' || typeof query !== 'function') return null;
  if (!isPositiveInt(workspace_id)) return null;
  const viewId = agent_view_id ?? null;
  if (viewId !== null) {
    if (!isPositiveInt(viewId)) return null;
    const [views] = await query(VIEW_WORKSPACE_SQL, [viewId]);
    if (views.length !== 1 || Number(views[0].workspace_id) !== workspace_id) return null;
  }
  const [launches] = await query(LAUNCH_SQL, [sourceId]);
  if (launches.length !== 1) return null;
  const l = launches[0];
  if (Number(l.workspace_id) !== workspace_id || (l.agent_view_id === null ? null : Number(l.agent_view_id)) !== viewId) {
    return null;
  }
  const [operations] = await query(GRANTS_SQL, [l.role, 'operation', viewId, workspace_id]);
  if (!operations.some(g => g.name === 'artifact.launch')) return null;
  const [grants] = await query(GRANTS_SQL, [l.role, 'tool', viewId, workspace_id]);
  if (grants.length === 0) return null;
  return {
    kind: 'launch',
    id: l.id,
    user_id: String(l.user_id),
    workspace_id,
    agent_view_id: viewId,
    permitted_tools: grants.map(g => g.name),
    created_at: Number(l.created_at),
    expires_at: Number(l.expires_at),
    launch_id: l.id,
    artifact_code: l.artifact_code,
    version_id: l.version_id,
  };
}

export const authSources = [['launch', checkLaunch]];
