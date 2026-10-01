// The `session` auth source (PRD E2 §4.2, E1 §3.2): the toolbox re-checks, on every
// `user_session` call, that the panel session behind the capability is still live and
// recomputes the user's grants for the capability's own scope (the framework's `grants`).

const SESSION_SQL =
  'SELECT s.id, s.user_id, u.role, UNIX_TIMESTAMP(s.created_at) AS created_at, ' +
  'UNIX_TIMESTAMP(s.expires_at) AS expires_at ' +
  'FROM session s JOIN `user` u ON u.id = s.user_id ' +
  'WHERE s.id = ? AND s.revoked_at IS NULL AND s.expires_at > NOW() AND u.is_active = 1';

const VIEW_WORKSPACE_SQL = 'SELECT workspace_id FROM agent_view WHERE id = ?';

const isPositiveInt = v => Number.isInteger(v) && v > 0;

// `grants(role, kind)` is the framework's grant rule for the capability's own scope.
export async function checkSession(sourceId, { capability_kind, workspace_id, agent_view_id, query, grants } = {}) {
  if (capability_kind !== 'user_session' || typeof sourceId !== 'string' || typeof query !== 'function'
      || typeof grants !== 'function') return null;
  if (!isPositiveInt(workspace_id)) return null;
  const viewId = agent_view_id ?? null;
  if (viewId !== null) {
    if (!isPositiveInt(viewId)) return null;
    const [views] = await query(VIEW_WORKSPACE_SQL, [viewId]);
    if (views.length !== 1 || Number(views[0].workspace_id) !== workspace_id) return null;
  }
  const [sessions] = await query(SESSION_SQL, [sourceId]);
  if (sessions.length !== 1) return null;
  const s = sessions[0];
  const tools = await grants(s.role, 'tool');
  // A user whose grants do not reach this scope has no business in it.
  if (tools.length === 0) return null;
  return {
    kind: 'session',
    id: s.id,
    user_id: String(s.user_id),
    workspace_id,
    agent_view_id: viewId,
    permitted_tools: tools,
    created_at: Number(s.created_at),
    expires_at: Number(s.expires_at),
  };
}

export const authSources = [['session', checkSession]];
