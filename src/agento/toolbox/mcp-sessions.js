/**
 * MCP session registry with an idle TTL.
 *
 * Sessions used to be removed ONLY through `transport.onclose`, i.e. only when a client
 * disconnected politely. The consumer kills a timed-out agent with SIGKILL
 * (`subprocess_runner.py`, TimeoutExpired -> proc.kill()), so no DELETE is ever sent and
 * the entry survived until the container restarted.
 *
 * This is NOT specific to any one harness — `/mcp` serves Codex and any other MCP client,
 * so every timed-out job leaked one — and it cannot be fixed client-side, because SIGKILL
 * leaves no opportunity to clean up. The server is the only place it can be fixed.
 *
 * Extracted from `server.js` so the sweep is unit-testable without booting the server.
 */

// Must comfortably exceed the longest job or a live session gets reaped mid-run. It is
// refreshed on every request for that session, so it bounds IDLE time, not total life.
export const DEFAULT_IDLE_MS = 2 * 60 * 60 * 1000;
export const DEFAULT_SWEEP_MS = 5 * 60 * 1000;

/** Run a cleanup call, swallowing both a synchronous throw and a rejected promise. */
function settle(fn) {
  try {
    const result = fn();
    if (result && typeof result.then === 'function') {
      result.then(undefined, () => {});
    }
  } catch {
    // best effort — the point is to drop the entry
  }
}


export class McpSessionRegistry {
  constructor({ idleMs = DEFAULT_IDLE_MS, now = () => Date.now(), logger = null } = {}) {
    this.idleMs = idleMs;
    this._now = now;
    this._logger = logger;
    this._sessions = new Map();
  }

  get size() {
    return this._sessions.size;
  }

  has(id) {
    return this._sessions.has(id);
  }

  get(id) {
    return this._sessions.get(id);
  }

  set(id, entry) {
    this._sessions.set(id, { ...entry, lastSeen: this._now() });
  }

  /** Refresh the idle clock. Called on every request that reuses a session. */
  touch(id) {
    const entry = this._sessions.get(id);
    if (entry) entry.lastSeen = this._now();
  }

  delete(id) {
    return this._sessions.delete(id);
  }

  /** Close and drop every session idle for longer than the TTL. Returns the count. */
  reap() {
    const now = this._now();
    let reaped = 0;
    for (const [id, entry] of this._sessions) {
      if (now - (entry.lastSeen ?? 0) < this.idleMs) continue;
      this._sessions.delete(id);
      reaped += 1;
      // Both close() calls may be async. Catching only the SYNCHRONOUS throw leaves a
      // rejected promise unhandled, which crashes the Toolbox process on
      // `unhandledRejection` — a sweep meant to protect the server would take it down.
      settle(() => entry.transport?.close?.());
      settle(() => entry.server?.close?.());
    }
    if (reaped > 0 && this._logger) this._logger(`reaped ${reaped} idle MCP session(s)`);
    return reaped;
  }

  /** Start periodic sweeping. The timer is unref'd so it never holds the process open. */
  startSweeper(sweepMs = DEFAULT_SWEEP_MS) {
    const timer = setInterval(() => this.reap(), sweepMs);
    timer.unref?.();
    return timer;
  }
}
