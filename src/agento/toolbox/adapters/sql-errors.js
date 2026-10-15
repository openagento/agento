// The agent decides whether to retry from this text alone, so a lock wait and a slow query must
// not look like a transient transport fault. Raw driver text ("Query inactivity timeout") made the
// agent retry the same blocked query over and over.

const MYSQL_LOCK_WAIT = new Set([1205]);
const MYSQL_STATEMENT_TIMEOUT = new Set([1969, 3024]);
const MSSQL_LOCK_TIMEOUT = 1222;

export function lockMessage() {
  return 'Query blocked by a lock on the server (not a query bug). The server-side statement was cancelled. '
    + 'Do not retry the same query; wait several minutes or report the database as blocked.';
}

export function timeoutMessage(seconds, { cancelled = true } = {}) {
  if (!cancelled) {
    return `Query exceeded ${seconds}s and could not be cancelled on the server, so it may still be running. `
      + 'Do not retry the same query; wait several minutes or report the database as blocked.';
  }
  return `Query exceeded ${seconds}s and was cancelled on the server. Retrying unchanged will time out again; `
    + 'narrow it (date range, LIMIT, indexed filters) or check EXPLAIN.';
}

function seconds(ms) {
  return Math.round(ms / 1000);
}

/**
 * @param {Error} err - mysql2 error; `err.cancelled` is set by the adapter after a client timeout
 * @param {{ clientTimeoutMs: number, statementTimeoutMs: number }} limits
 * @returns {string|null} Actionable message, or null when the raw driver error is the best answer
 */
export function describeMysqlError(err, { clientTimeoutMs, statementTimeoutMs }) {
  if (MYSQL_LOCK_WAIT.has(err.errno)) return lockMessage();
  if (MYSQL_STATEMENT_TIMEOUT.has(err.errno)) return timeoutMessage(seconds(statementTimeoutMs));
  if (err.code === 'PROTOCOL_SEQUENCE_TIMEOUT') {
    return timeoutMessage(seconds(clientTimeoutMs), { cancelled: err.cancelled === true });
  }
  return null;
}

/**
 * @param {Error} err - mssql RequestError
 * @param {{ clientTimeoutMs: number }} limits
 * @returns {string|null}
 */
export function describeMssqlError(err, { clientTimeoutMs }) {
  if (err.number === MSSQL_LOCK_TIMEOUT) return lockMessage();
  // tedious answers its request timeout with request.cancel(), i.e. a TDS ATTENTION packet, so the
  // server stops the statement.
  if (err.code === 'ETIMEOUT') return timeoutMessage(seconds(clientTimeoutMs));
  return null;
}
