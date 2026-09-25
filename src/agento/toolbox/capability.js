import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { errorCategory } from './log.js';
import { getCronPool } from './db.js';
import {
  AuthConfigError, computeAuthTtls, deriveAuthContext, ENDPOINT_TRANSPORT, SINGLE_USE_KINDS,
} from './auth-context.js';

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

// CAST(... AS CHAR) in the SQL, not String() in JS: mysql2 has already rounded a BIGINT past
// 2^53 by the time the row reaches us, so stringifying afterwards preserves the WRONG number.
// The conversion has to happen in the database. Timestamps come back as epoch seconds, so the
// lifetime checks in deriveAuthContext compare two values from one clock.
const ROW_SQL =
  'SELECT CAST(c.id AS CHAR) AS id, c.kind, c.actor, c.subject_id, c.on_behalf_of, c.agent_view_id, ' +
  'c.workspace_id, CAST(c.job_id AS CHAR) AS job_id, c.execution_id, c.app_artifact_code, ' +
  'c.app_version_id, c.app_launch_id, c.tool_ceiling, c.allowed_transports, c.source_kind, c.source_id, ' +
  'UNIX_TIMESTAMP(c.created_at) AS created_at, UNIX_TIMESTAMP(c.expires_at) AS expires_at, ' +
  'av.workspace_id AS agent_view_workspace_id ' +
  'FROM toolbox_capability c LEFT JOIN agent_view av ON av.id = c.agent_view_id ' +
  'WHERE c.revoked_at IS NULL AND c.expires_at > NOW() AND ';

const SELECT_BY_HASH_SQL = `${ROW_SQL}c.token_hash = ? LIMIT 1`;
const SELECT_BY_ID_SQL = `${ROW_SQL}c.id = ? LIMIT 1`;

// mysql2 parses a JSON column by default, but a driver option or a fake can hand over the
// text; either way the derivation sees an array or a rejection, never a string.
function jsonColumn(v) {
  if (typeof v !== 'string') return v ?? null;
  try {
    return JSON.parse(v);
  } catch {
    return undefined;
  }
}

function intColumn(v) {
  if (v === null || v === undefined) return null;
  const n = typeof v === 'string' && /^-?[0-9]+$/.test(v) ? Number(v) : v;
  return typeof n === 'number' && Number.isSafeInteger(n) ? n : undefined;
}

function normalizeRow(row) {
  return {
    ...row,
    agent_view_id: intColumn(row.agent_view_id),
    workspace_id: intColumn(row.workspace_id),
    created_at: intColumn(row.created_at),
    expires_at: intColumn(row.expires_at),
    tool_ceiling: jsonColumn(row.tool_ceiling),
    allowed_transports: jsonColumn(row.allowed_transports),
  };
}

// The source checkers for user_session and miniapp, discovered once at startup (see
// registerAuthSources). Exposed only as a `lookup` closure: a frozen Map still answers
// `Map.prototype.set`, so the registry is never handed out as a Map at all. Until
// installAuthSources runs, the lookup answers nothing and every new-profile row is refused.
export const NO_AUTH_SOURCES = Object.freeze({ lookup: () => null });

export function createSourceLookup(entries) {
  const table = new Map(entries);
  return Object.freeze({ lookup: kind => (table.has(kind) ? table.get(kind) : null) });
}

export function createVerifier(query, { sourceCheckers = NO_AUTH_SOURCES, resolveTtls } = {}) {
  const ttls = resolveTtls || (workspaceId => resolveAuthTtls(query, workspaceId));

  async function derive(row, endpoint) {
    if (!row) return null;
    const normalized = normalizeRow(row);
    let source = null;
    let ttlCaps = null;
    if (SINGLE_USE_KINDS.includes(normalized.kind)) {
      // Both the capability and its source are checked on every call: a checker that is
      // missing, or that no longer finds a live session/launch, rejects an unexpired token.
      const check = sourceCheckers.lookup(normalized.source_kind);
      if (typeof check !== 'function' || typeof normalized.source_id !== 'string') return null;
      source = await check(normalized.source_id, { capability_kind: normalized.kind });
      if (!source) return null;
      ttlCaps = await ttls(normalized.workspace_id);
    }
    return deriveAuthContext({
      row: normalized,
      agentViewWorkspaceId: intColumn(row.agent_view_workspace_id) ?? null,
      source,
      endpoint,
      ttlCaps,
    });
  }

  // `endpoint` names the route presenting the token; the per-kind table in auth-context.js
  // decides whether this kind, over this transport, may be presented there. A guard that
  // names no endpoint authenticates nothing.
  async function verifyCapability(token, { endpoint } = {}) {
    if (typeof token !== 'string' || token.length === 0) return null;
    if (!Object.hasOwn(ENDPOINT_TRANSPORT, endpoint)) return null;
    const [rows] = await query(SELECT_BY_HASH_SQL, [tokenHash(token)]);
    return derive(rows && rows[0], endpoint);
  }

  // The per-call re-verification the dispatcher runs: the same row, by id, through the same
  // derivation — so a revoke, an expiry or a withdrawn source lands on the next call.
  verifyCapability.reverify = async (capabilityId, { endpoint } = {}) => {
    if (typeof capabilityId !== 'string' || !/^[1-9][0-9]*$/.test(capabilityId)) return null;
    if (!Object.hasOwn(ENDPOINT_TRANSPORT, endpoint)) return null;
    const [rows] = await query(SELECT_BY_ID_SQL, [capabilityId]);
    return derive(rows && rows[0], endpoint);
  };

  return verifyCapability;
}

const coreConfigFile = () =>
  path.join(process.env.CORE_MODULES_DIR || '/app/modules/core', 'core', 'config.json');

// The three core/auth/* bounds for a workspace, strictly: a failed query throws (the guard
// answers 503) instead of reading as "unset", which would silently undo a narrowed TTL. Only
// default and workspace rows are read — never agent_view. Clamped in computeAuthTtls.
export async function resolveAuthTtls(query, workspaceId) {
  const layer = async (scope, scopeId) => {
    const [rows] = await query(
      "SELECT path, value, encrypted FROM core_config_data WHERE scope = ? AND scope_id = ? AND path LIKE 'core/auth/%'",
      [scope, scopeId]
    );
    const out = {};
    for (const row of rows) {
      if (row.encrypted) throw new AuthConfigError(`${row.path} must not be stored encrypted`);
      out[row.path] = row.value;
    }
    return out;
  };
  const defaultOverrides = await layer('default', 0);
  const workspaceOverrides = Number.isInteger(workspaceId) ? await layer('workspace', workspaceId) : {};
  let configDefaults = {};
  try {
    configDefaults = JSON.parse(fs.readFileSync(coreConfigFile(), 'utf8'));
  } catch {
    configDefaults = {};
  }
  return computeAuthTtls({ env: process.env, defaultOverrides, workspaceOverrides, configDefaults });
}

// Endpoints where a query-string token is refused outright — the header is the only source.
const HEADER_ONLY_ENDPOINTS = ['invoke'];

export function createRequireCapability(verify, { endpoint }, log) {
  return async function requireCapability(req, res, next) {
    if (HEADER_ONLY_ENDPOINTS.includes(endpoint) && req?.query?.cap !== undefined) {
      log('auth', 'ERROR', `query capability refused for ${req.method} ${req.path || ''}`);
      return res.status(401).json({ error: 'capability must be sent in the Authorization header' });
    }
    const token = extractToken(req);
    if (!token) {
      log('auth', 'ERROR', `missing capability for ${req.method} ${req.path || ''}`);
      return res.status(401).json({ error: 'capability required' });
    }
    let derived = null;
    try {
      derived = await verify(token, { endpoint });
    } catch (err) {
      log('auth', 'ERROR', `capability verification failed: ${errorCategory(err)}`);
      return res.status(503).json({ error: 'capability verification unavailable' });
    }
    if (!derived) {
      log('auth', 'ERROR', `invalid capability for ${req.method} ${req.path || ''}`);
      return res.status(403).json({ error: 'invalid capability' });
    }
    req.capability = derived.context;
    return next();
  };
}

// Set ONCE, at startup, by installAuthSources — never from a request or session handler
// (RULES.md:35). The default verifier reads it through a closure, so a lookup made before
// discovery completes answers nothing.
let installedAuthSources = NO_AUTH_SOURCES;

export function installAuthSources(lookup) {
  if (installedAuthSources !== NO_AUTH_SOURCES) throw new Error('auth sources are already installed');
  installedAuthSources = lookup;
}

const defaultSourceCheckers = Object.freeze({ lookup: kind => installedAuthSources.lookup(kind) });

// Process singleton, initialised ONCE at module load and never rebound. RULES.md:35 —
// a module-level `let` written from a request/session handler cross-contaminates sessions.
// The pool lookup stays inside the query closure, so module load does not force a connection.
const defaultVerifier = createVerifier(
  (sql, params) => getCronPool().query(sql, params),
  { sourceCheckers: defaultSourceCheckers }
);

export function verifyCapability(token, opts) {
  return defaultVerifier(token, opts);
}

export function reverifyCapability(capabilityId, opts) {
  return defaultVerifier.reverify(capabilityId, opts);
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
  const scope = req.capability?.agent_view_id;
  const supplied = [req.body?.agent_view_id, req.query?.agent_view_id].filter(
    v => v !== null && v !== undefined
  );
  if (supplied.length === 0) return false;
  if (supplied.every(v => Number(v) === scope)) return false;
  log(route, 'ERROR', 'supplied agent_view_id disagrees with the capability');
  res.status(400).json({ error: 'agent_view_id does not match the capability' });
  return true;
}
