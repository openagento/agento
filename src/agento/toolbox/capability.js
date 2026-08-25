import { createHash } from 'node:crypto';
import { errorCategory } from './log.js';
import { getCronPool } from './db.js';

export function tokenHash(token) {
  return createHash('sha256').update(String(token)).digest('hex');
}

// Authorization: Bearer <t> is preferred (never lands in a URL or a proxy access log).
// The ?cap=<t> query param exists because MCP clients configure a bare URL and cannot
// set headers; it is why `cap` must never be echoed into any log line.
export function extractToken(req) {
  const auth = req?.headers?.authorization;
  if (typeof auth === 'string') {
    const m = auth.match(/^Bearer\s+(\S+)$/i);
    if (m) return m[1];
  }
  const q = req?.query?.cap;
  return typeof q === 'string' && q.length > 0 ? q : null;
}

// CAST(job_id AS CHAR) in the SQL, not String() in JS: mysql2 has already rounded a
// BIGINT past 2^53 by the time the row reaches us, so stringifying afterwards preserves
// the WRONG number. The conversion has to happen in the database.
const KIND_MCP_JOB = 'mcp_job';
const KIND_MCP_INTERACTIVE = 'mcp_interactive';
const KIND_INTERNAL_REST = 'internal_rest';

const SELECT_SQL =
  'SELECT kind, agent_view_id, CAST(job_id AS CHAR) AS job_id FROM toolbox_capability ' +
  'WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > NOW() LIMIT 1';

export function createVerifier(query) {
  return async function verifyCapability(token, { kinds, allowViewless = false }) {
    if (typeof token !== 'string' || token.length === 0) return null;
    const [rows] = await query(SELECT_SQL, [tokenHash(token)]);
    const row = rows && rows[0];
    if (!row) return null;
    // Fail CLOSED on a missing or non-array `kinds`: a guard that forgot to declare its
    // kinds must authenticate nothing, not everything. RULES.md's no-sentinel rule applied
    // to the kind check.
    if (!Array.isArray(kinds) || kinds.length === 0 || !kinds.includes(row.kind)) return null;
    // FAIL-CLOSED: loadScopedDbOverrides falls back to GLOBAL config for a falsy
    // agent_view_id, which is wider than any caller may hold. A row without a
    // positive view is not a usable capability — except a viewless `internal_rest`
    // row at a guard that opted in with `allowViewless` (`/config-test` at the default
    // scope). Every other guard still refuses it.
    const viewless = row.agent_view_id === null || row.agent_view_id === undefined;
    if (viewless) {
      if (!allowViewless || row.kind !== KIND_INTERNAL_REST) return null;
    }
    const agentViewId = viewless ? null : Number(row.agent_view_id);
    if (!viewless && (!Number.isInteger(agentViewId) || agentViewId <= 0)) return null;
    // job.id is BIGINT UNSIGNED (`sql/001_create_tables.sql:16`), already CAST to CHAR by the
    // query. A `mcp_job` row whose job_id is NULL must be REJECTED, not passed through as null:
    // Outlook reads a null jobId as the interactive escape hatch and drops job-message binding,
    // so a malformed row would silently widen a job token into an unbound one.
    //
    // Per-kind invariants — `internal_rest` is deliberately BOTH, because a job-owned
    // discovery capability carries the job's id so the terminal transition revokes it by
    // job_id, while standalone publisher/observer capabilities own no job:
    //   mcp_job         -> a positive exact id, required
    //   mcp_interactive -> null, always
    //   internal_rest   -> null, or a positive exact id
    const jobId = row.job_id === null || row.job_id === undefined ? null : String(row.job_id);
    const isPositiveId = jobId !== null && /^[1-9][0-9]*$/.test(jobId);
    if (row.kind === KIND_MCP_JOB && !isPositiveId) return null;
    if (row.kind === KIND_MCP_INTERACTIVE && jobId !== null) return null;
    if (row.kind === KIND_INTERNAL_REST && jobId !== null && !isPositiveId) return null;
    // Same rule as issue_capability: a viewless capability owns no job.
    if (viewless && jobId !== null) return null;
    return { kind: row.kind, agentViewId, jobId };
  };
}

export function createRequireCapability(verify, { kinds, allowViewless = false }, log) {
  return async function requireCapability(req, res, next) {
    const token = extractToken(req);
    if (!token) {
      log('auth', 'ERROR', `missing capability for ${req.method} ${req.path || ''}`);
      return res.status(401).json({ error: 'capability required' });
    }
    let claims = null;
    try {
      claims = await verify(token, { kinds, allowViewless });
    } catch (err) {
      log('auth', 'ERROR', `capability verification failed: ${errorCategory(err)}`);
      return res.status(503).json({ error: 'capability verification unavailable' });
    }
    if (!claims) {
      log('auth', 'ERROR', `invalid capability for ${req.method} ${req.path || ''}`);
      return res.status(403).json({ error: 'invalid capability' });
    }
    req.capability = claims;
    return next();
  };
}

// Process singleton, initialised ONCE at module load and never rebound. RULES.md:35 —
// a module-level `let` written from a request/session handler cross-contaminates sessions.
// The pool lookup stays inside the query closure, so module load does not force a connection.
const defaultVerifier = createVerifier((sql, params) => getCronPool().query(sql, params));

export function verifyCapability(token, opts) {
  return defaultVerifier(token, opts);
}

export function requireCapability(opts, log) {
  return createRequireCapability((t, o) => verifyCapability(t, o), opts, log);
}

// A request body may still carry agent_view_id (older callers, and it keeps the log line
// readable), but it can only AGREE with the capability — never select the scope. Returns
// true when it already answered 400, so a caller that forgets the check still cannot widen
// its scope: the body value is not used for scoping anywhere.
export function rejectScopeMismatch(req, res, log, route) {
  // Body AND query string, independently: a caller-supplied scope is never USED (the
  // capability row is the only source), but silently ignoring one answers a different view
  // than the caller asked about. `body ?? query` would hide a query that disagrees behind a
  // body that agrees, so EVERY supplied value is compared and any disagreement is a 400.
  const scope = req.capability?.agentViewId;
  const supplied = [req.body?.agent_view_id, req.query?.agent_view_id].filter(
    v => v !== null && v !== undefined
  );
  if (supplied.length === 0) return false;
  if (supplied.every(v => Number(v) === scope)) return false;
  log(route, 'ERROR', 'supplied agent_view_id disagrees with the capability');
  res.status(400).json({ error: 'agent_view_id does not match the capability' });
  return true;
}
