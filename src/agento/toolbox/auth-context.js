// Auth context v1: the one structure every verified capability yields, whichever
// transport presented it. Pure — the verifier does the I/O and hands this the row it read,
// the agent_view's workspace from the join, and (for user_session/miniapp) the source record
// its checker returned. Every rule is fail-closed: a claim the table below cannot derive is
// a null result, never a permissive default. The Python twin is
// src/agento/framework/auth_context.py; both are held to tests/fixtures/auth_context_v1.json.

export const LEGACY_REST_SUBJECT = 'service:legacy-internal-rest';

export const TTL_CEILINGS = Object.freeze({
  session_max_ttl: 86400,
  launch_max_ttl: 43200,
  capability_ttl: 300,
});

export const TTL_DEFAULTS = Object.freeze({
  session_max_ttl: 43200,
  launch_max_ttl: 3600,
  capability_ttl: 30,
});

export const ENDPOINT_TRANSPORT = Object.freeze({
  sse: 'sse',
  messages: 'sse',
  mcp: 'http',
  invoke: 'http',
  api: 'http',
  config_test: 'http',
  health: 'http',
});

export const ENDPOINTS = Object.freeze(Object.keys(ENDPOINT_TRANSPORT));

const MCP_ENDPOINTS = ['sse', 'messages', 'mcp', 'invoke'];

const KINDS = {
  mcp_job: { actor: 'agent', endpoints: MCP_ENDPOINTS },
  mcp_interactive: { actor: 'agent', endpoints: MCP_ENDPOINTS },
  internal_rest: { actor: 'service', endpoints: ['api', 'config_test', 'health'] },
  user_session: { actor: 'user', endpoints: ['invoke'], source: 'session', ttl: 'session_max_ttl' },
  miniapp: { actor: 'user', endpoints: ['invoke'], source: 'launch', ttl: 'launch_max_ttl' },
};

export const SINGLE_USE_KINDS = Object.freeze(['user_session', 'miniapp']);

const TRANSPORTS = ['http', 'sse'];

const isPositiveInt = v => typeof v === 'number' && Number.isInteger(v) && v > 0;
const isText = v => typeof v === 'string' && v.length > 0;
const isNull = v => v === null || v === undefined;
const isDecimalId = v => typeof v === 'string' && /^[1-9][0-9]*$/.test(v);
const isStringList = v => Array.isArray(v) && v.every(isText);

function lifetimeWithin(createdAt, expiresAt, cap) {
  if (!Number.isInteger(createdAt) || !Number.isInteger(expiresAt) || !isPositiveInt(cap)) return false;
  const lifetime = expiresAt - createdAt;
  return lifetime > 0 && lifetime <= cap;
}

function transportsValid(list) {
  return Array.isArray(list) && list.length > 0 &&
    list.every(t => TRANSPORTS.includes(t)) && new Set(list).size === list.length;
}

// A user id may be an integer or a string upstream; the subject is always its string form.
function subjectOf(userId) {
  if (isPositiveInt(userId)) return String(userId);
  return isText(userId) ? userId : null;
}

function checkSource(row, source, profile, ttlCaps) {
  if (!source || typeof source !== 'object') return false;
  if (row.source_kind !== profile.source || !isText(row.source_id)) return false;
  if (source.kind !== profile.source || subjectOf(source.id) !== row.source_id) return false;
  const subject = subjectOf(source.user_id);
  if (subject === null || row.subject_id !== subject) return false;
  if (source.workspace_id !== row.workspace_id) return false;
  if ((source.agent_view_id ?? null) !== (row.agent_view_id ?? null)) return false;
  if (!isStringList(source.permitted_tools)) return false;
  if (!lifetimeWithin(source.created_at, source.expires_at, ttlCaps?.[profile.ttl])) return false;
  if (!lifetimeWithin(row.created_at, row.expires_at, ttlCaps?.capability_ttl)) return false;
  // The capability never outlives the session or launch it was minted from.
  return row.expires_at <= source.expires_at;
}

export function deriveAuthContext({ row, agentViewWorkspaceId = null, source = null, endpoint, ttlCaps }) {
  if (!row || typeof row !== 'object') return null;
  const profile = Object.hasOwn(KINDS, row.kind) ? KINDS[row.kind] : null;
  if (!profile) return null;
  if (!Object.hasOwn(ENDPOINT_TRANSPORT, endpoint)) return null;

  if (!transportsValid(row.allowed_transports)) return null;
  if (!row.allowed_transports.includes(ENDPOINT_TRANSPORT[endpoint])) return null;

  const viewless = isNull(row.agent_view_id);
  const endpoints = row.kind === 'internal_rest' && viewless ? ['config_test'] : profile.endpoints;
  if (!endpoints.includes(endpoint)) return null;

  if (row.actor !== profile.actor) return null;
  // Nothing verifies delegation yet, so no profile may claim it.
  if (!isNull(row.on_behalf_of)) return null;

  if (!viewless && !isPositiveInt(row.agent_view_id)) return null;
  if (!isNull(row.workspace_id) && !isPositiveInt(row.workspace_id)) return null;
  if (!viewless && (isNull(row.workspace_id) || row.workspace_id !== agentViewWorkspaceId)) return null;
  if (!isNull(row.job_id) && !isDecimalId(row.job_id)) return null;
  if (!isNull(row.execution_id) && !isText(row.execution_id)) return null;
  if (!Number.isInteger(row.expires_at)) return null;

  const hasApp = !isNull(row.app_artifact_code) || !isNull(row.app_version_id) || !isNull(row.app_launch_id);
  const hasSource = !isNull(row.source_kind) || !isNull(row.source_id);
  const agentOnlyNulls = !hasApp && isNull(row.tool_ceiling) && !hasSource;
  let permittedTools = null;

  switch (row.kind) {
    case 'mcp_job':
      if (viewless || !isDecimalId(row.job_id) || !agentOnlyNulls) return null;
      if (row.subject_id !== String(row.agent_view_id)) return null;
      break;
    case 'mcp_interactive':
      if (viewless || !isNull(row.job_id) || !isNull(row.execution_id) || !agentOnlyNulls) return null;
      if (row.subject_id !== String(row.agent_view_id)) return null;
      break;
    case 'internal_rest':
      if (!agentOnlyNulls || !isNull(row.execution_id)) return null;
      if (!isText(row.subject_id) || !row.subject_id.startsWith('service:') || row.subject_id.length <= 8) return null;
      if (viewless && (!isNull(row.workspace_id) || !isNull(row.job_id))) return null;
      break;
    case 'user_session':
    case 'miniapp':
      if (!isPositiveInt(row.workspace_id)) return null;
      if (!isNull(row.job_id) || !isNull(row.execution_id)) return null;
      if (row.kind === 'user_session') {
        if (hasApp || !isNull(row.tool_ceiling)) return null;
      } else {
        if (!isText(row.app_artifact_code) || !isPositiveInt(row.app_version_id) || !isText(row.app_launch_id)) return null;
        if (!isStringList(row.tool_ceiling)) return null;
        if (source?.launch_id !== row.app_launch_id || source?.artifact_code !== row.app_artifact_code ||
            source?.version_id !== row.app_version_id) return null;
      }
      if (!checkSource(row, source, profile, ttlCaps)) return null;
      permittedTools = [...source.permitted_tools];
      break;
    default:
      return null;
  }

  const context = {
    actor: profile.actor,
    subject_id: row.subject_id,
    on_behalf_of: null,
    agent_view_id: viewless ? null : row.agent_view_id,
    workspace_id: isNull(row.workspace_id) ? null : row.workspace_id,
    job_id: isNull(row.job_id) ? null : row.job_id,
    execution_id: isNull(row.execution_id) ? null : row.execution_id,
    app: hasApp
      ? { artifact_code: row.app_artifact_code, version_id: row.app_version_id, launch_id: row.app_launch_id }
      : null,
    tool_ceiling: isNull(row.tool_ceiling) ? null : [...row.tool_ceiling],
    allowed_transports: [...row.allowed_transports],
    kind: row.kind,
    expires_at: row.expires_at,
    capability_id: isNull(row.id) ? null : String(row.id),
  };
  return { context, single_use: SINGLE_USE_KINDS.includes(row.kind), permitted_tools: permittedTools };
}

export const AUTH_TTL_KEYS = Object.freeze(Object.keys(TTL_DEFAULTS));

export class AuthConfigError extends Error {
  constructor(message) {
    super(message);
    this.name = 'AuthConfigError';
  }
}

export const authTtlPath = key => `core/auth/${key}`;
export const authTtlEnvKey = key => `CONFIG__CORE__AUTH__${key.toUpperCase()}`;

const clampWarned = new Set();

// The pure half of the core/auth/* resolution, shared with Python through the fixture.
// ENV -> workspace row -> default row -> config.json -> code default. An agent_view row is
// never an input: a view-scoped security bound would let whoever configures a view widen it.
// A present value that is not a positive integer is an error, never a fallback, and the
// result is clamped to the ceiling in code — config can only narrow.
export function computeAuthTtls({ env = {}, defaultOverrides = {}, workspaceOverrides = {}, configDefaults = {} }) {
  const caps = {};
  for (const key of AUTH_TTL_KEYS) {
    const p = authTtlPath(key);
    let raw = env[authTtlEnvKey(key)];
    if (raw === undefined || raw === null) raw = workspaceOverrides[p];
    if (raw === undefined || raw === null) raw = defaultOverrides[p];
    if (raw === undefined || raw === null) raw = configDefaults[`auth/${key}`] ?? TTL_DEFAULTS[key];
    const text = typeof raw === 'boolean' ? '' : String(raw).trim();
    if (!/^[1-9][0-9]*$/.test(text)) throw new AuthConfigError(`${p} must be a positive integer number of seconds`);
    const value = Number(text);
    if (value > TTL_CEILINGS[key] && !clampWarned.has(key)) {
      clampWarned.add(key);  // at most one entry per AUTH_TTL_KEYS name
      console.warn(`[auth] ${p}=${value} exceeds the hard ceiling; clamped to ${TTL_CEILINGS[key]}`);
    }
    caps[key] = Math.min(value, TTL_CEILINGS[key]);
  }
  return caps;
}
