const DEFAULT_TIMEOUT_SECONDS = 300;

export function getSqlTimeoutMs(seconds = DEFAULT_TIMEOUT_SECONDS) {
  const parsed = Number(seconds);
  const resolvedSeconds = Number.isFinite(parsed) && parsed >= 0
    ? parsed
    : DEFAULT_TIMEOUT_SECONDS;
  return resolvedSeconds * 1000;
}

const DEFAULT_LOCK_WAIT_TIMEOUT_SECONDS = 60;
// The server must give up before the client does, so the agent gets the server's precise error
// (lock wait, statement timeout) instead of a bare client timeout.
const STATEMENT_TIMEOUT_MARGIN_MS = 5_000;

function nonNegativeInteger(value) {
  const parsed = Number.parseInt(value, 10);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : null;
}

/**
 * Server-side limits for one SQL tool. 0 means "no limit" for the statement timeout.
 * @param {number} sqlTimeoutMs - Client-side timeout (core/sql_timeout_seconds)
 * @param {{ statementTimeoutSeconds?, lockWaitTimeoutSeconds? }} configured - Per-tool or core values
 */
export function getSqlServerLimits(sqlTimeoutMs, { statementTimeoutSeconds, lockWaitTimeoutSeconds } = {}) {
  const statement = nonNegativeInteger(statementTimeoutSeconds);
  const lockWait = nonNegativeInteger(lockWaitTimeoutSeconds);
  return {
    statementTimeoutMs: statement !== null
      ? statement * 1000
      : Math.max(sqlTimeoutMs - STATEMENT_TIMEOUT_MARGIN_MS, Math.min(sqlTimeoutMs, 1000)),
    lockWaitTimeoutSeconds: lockWait > 0 ? lockWait : DEFAULT_LOCK_WAIT_TIMEOUT_SECONDS,
  };
}
