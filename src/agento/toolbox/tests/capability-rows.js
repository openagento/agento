// A verifier row as the capability SELECT returns it (auth context v1 columns plus the
// agent_view join), for tests that drive the REAL verifier through a fake query.
const T0 = 1790000000;

const BASE = {
  on_behalf_of: null, execution_id: null, app_artifact_code: null, app_version_id: null,
  app_launch_id: null, tool_ceiling: null, source_kind: null, source_id: null,
  created_at: T0, expires_at: T0 + 3600,
};

export function capabilityRow(kind, overrides = {}) {
  const view = overrides.agent_view_id === undefined ? 7 : overrides.agent_view_id;
  const byKind = {
    mcp_job: { actor: 'agent', subject_id: String(view), job_id: '42', allowed_transports: ['http'] },
    mcp_interactive: { actor: 'agent', subject_id: String(view), job_id: null, allowed_transports: ['http'] },
    internal_rest: { actor: 'service', subject_id: 'service:test', job_id: null, allowed_transports: ['http'] },
  }[kind];
  return {
    id: '1', kind, ...BASE, ...byKind,
    agent_view_id: view,
    workspace_id: view === null ? null : 3,
    agent_view_workspace_id: view === null ? null : 3,
    ...overrides,
  };
}

// What the guard attaches to req.capability for such a row.
export function capabilityContext(kind, overrides = {}) {
  const row = capabilityRow(kind, overrides);
  return {
    actor: row.actor, subject_id: row.subject_id, on_behalf_of: null,
    agent_view_id: row.agent_view_id, workspace_id: row.workspace_id, job_id: row.job_id,
    execution_id: row.execution_id, app: null, tool_ceiling: null,
    allowed_transports: row.allowed_transports, kind, expires_at: row.expires_at, capability_id: row.id,
  };
}
