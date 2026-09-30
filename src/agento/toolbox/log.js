import { appendFileSync } from 'node:fs';

const MCP_LOG_FILE = process.env.MCP_LOG_FILE || '/app/logs/toolbox_mcp.log';
const REST_LOG_FILE = process.env.REST_LOG_FILE || '/app/logs/toolbox_rest.log';
const PUBLISHER_LOG_FILE = process.env.PUBLISHER_LOG_FILE || '/app/logs/publisher.log';

// ONE bounded line, applied by every sink to the caller-supplied `details`.
// A record is newline-delimited, so an unsanitized detail lets any caller write a
// SECOND record an operator cannot tell from a genuine one. Sanitizing here rather
// than at the ~200 call sites is what makes that impossible by construction.
//   - whitespace collapse: nothing becomes several records.
//   - control-character strip: `\s` does not match NUL, and a NUL or an ESC
//     sequence in a log file is a record nobody can read or grep.
//   - final slice: bounds a flooded detail.
// This is the SHAPE half only. It does not decide WHICH values may appear — a
// bounded secret is still a leaked secret; that call belongs at each sink.
const MAX_DETAILS = 2000;

export function boundedDetails(text) {
  return String(text ?? '')
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, MAX_DETAILS);
}

function _write(file, tool, status, details = '') {
  const ts = new Date().toISOString();
  const line = `[${ts}] [${tool}] ${status} ${boundedDetails(details)}`;
  console.log(line);
  try {
    appendFileSync(file, line + '\n');
  } catch { /* file write is best-effort */ }
}

// Agent MCP tool invocations -> toolbox_mcp.log (kept clean of REST/lifecycle noise).
export function logToolboxMcp(tool, status, details = '') {
  _write(MCP_LOG_FILE, tool, status, details);
}

// REST /api/* calls + lifecycle/discovery/startup noise -> toolbox_rest.log.
export function logToolboxRest(tool, status, details = '') {
  _write(REST_LOG_FILE, tool, status, details);
}

export function logPublisher(tool, status, details = '') {
  _write(PUBLISHER_LOG_FILE, tool, status, details);
}

// Per-session logger for the MCP path: registration-time diagnostics (e.g. a module's
// browser SESSION/INIT emitted from register()) route to toolbox_rest.log, then it flips
// to the given MCP invocation logger so only real tool invocations reach toolbox_mcp.log.
// State is per session (one instance per createServer call), so it is safe under concurrent
// sessions. The flip happens after registration completes and before any handler can run.
export function createPhasedLogger(invocationLog) {
  let invoking = false;
  const log = (tool, status, details = '') => {
    (invoking ? invocationLog : logToolboxRest)(tool, status, details);
  };
  log.toInvocationPhase = () => { invoking = true; };
  return log;
}

// Agent_view-scoped MCP tool logger -> toolbox_mcp.log.
export function createScopedLogger(agentViewMeta) {
  return function scopedLogToolbox(tool, status, details = '') {
    const ts = new Date().toISOString();
    const prefix = agentViewMeta
      ? `[${agentViewMeta.label} (id: ${agentViewMeta.id})] `
      : '';
    const line = `[${ts}] ${prefix}[${tool}] ${status} ${boundedDetails(details)}`;
    console.log(line);
    try {
      appendFileSync(MCP_LOG_FILE, line + '\n');
    } catch { /* best-effort */ }
  };
}

// A caught error's `message` is free text written by a driver: mysql2 puts the connection
// target in it, a Graph failure quotes the credential it used, an HTTP client embeds the URL
// (and any token in it). Logging it is the same leak as returning it, so every new boundary
// logs a STABLE CATEGORY instead — the error's own code/name, which is an identifier, not text.
export function errorCategory(err) {
  if (err === null || err === undefined) return 'unknown';
  const code = typeof err.code === 'string' ? err.code : null;
  const name = typeof err.name === 'string' ? err.name : null;
  return code || name || 'Error';
}
