/**
 * The MCP session registry's idle TTL.
 *
 * Regression cover for a leak that affected EVERY harness, not just Pi: sessions were
 * dropped only via `transport.onclose`, but the consumer SIGKILLs a timed-out agent, so
 * no DELETE is ever sent and the entry survived until the container restarted.
 */
import { describe, expect, it, vi } from 'vitest';

import { McpSessionRegistry } from '../mcp-sessions.js';

function entry() {
  return { transport: { close: vi.fn() }, server: { close: vi.fn(async () => {}) } };
}

describe('McpSessionRegistry', () => {
  it('keeps a session that is still within its idle window', () => {
    let now = 1000;
    const reg = new McpSessionRegistry({ idleMs: 500, now: () => now });
    reg.set('s1', entry());
    now = 1400;
    expect(reg.reap()).toBe(0);
    expect(reg.has('s1')).toBe(true);
  });

  it('reaps a session abandoned without DELETE — the SIGKILL case', () => {
    let now = 1000;
    const reg = new McpSessionRegistry({ idleMs: 500, now: () => now });
    const e = entry();
    reg.set('s1', e);
    now = 1600; // client was killed; no DELETE ever arrives
    expect(reg.reap()).toBe(1);
    expect(reg.has('s1')).toBe(false);
    expect(e.transport.close).toHaveBeenCalled();
    expect(e.server.close).toHaveBeenCalled();
  });

  it('touch() refreshes the clock, so a long but ACTIVE session is never reaped', () => {
    let now = 1000;
    const reg = new McpSessionRegistry({ idleMs: 500, now: () => now });
    reg.set('s1', entry());
    for (let i = 0; i < 10; i += 1) {
      now += 400;
      reg.touch('s1');
      expect(reg.reap()).toBe(0);
    }
    expect(reg.has('s1')).toBe(true);
  });

  it('reaps only the idle sessions, leaving the fresh ones', () => {
    let now = 1000;
    const reg = new McpSessionRegistry({ idleMs: 500, now: () => now });
    reg.set('old', entry());
    now = 1600;
    reg.set('new', entry());
    expect(reg.reap()).toBe(1);
    expect(reg.has('old')).toBe(false);
    expect(reg.has('new')).toBe(true);
  });

  it('survives a transport whose close() throws', () => {
    let now = 1000;
    const reg = new McpSessionRegistry({ idleMs: 1, now: () => now });
    reg.set('s1', { transport: { close: () => { throw new Error('boom'); } }, server: {} });
    now = 5000;
    expect(() => reg.reap()).not.toThrow();
    expect(reg.size).toBe(0);
  });

  it('does not hold the process open', () => {
    const reg = new McpSessionRegistry();
    const timer = reg.startSweeper(10_000);
    expect(timer.unref).toBeDefined();
    clearInterval(timer);
  });
});

describe('async cleanup safety', () => {
  it('survives a transport whose close() REJECTS', async () => {
    // Catching only the synchronous throw left a rejected promise unhandled, which
    // crashes the Toolbox on `unhandledRejection` — a sweep meant to protect the server
    // would have taken it down.
    let now = 1000;
    const reg = new McpSessionRegistry({ idleMs: 1, now: () => now });
    reg.set('s1', {
      transport: { close: () => Promise.reject(new Error('async close failed')) },
      server: { close: () => Promise.reject(new Error('async server close failed')) },
    });
    now = 5000;

    const seen = [];
    const onUnhandled = (err) => seen.push(err);
    process.on('unhandledRejection', onUnhandled);
    expect(reg.reap()).toBe(1);
    // Let the microtask queue drain so a genuinely unhandled rejection would surface.
    await new Promise((resolve) => setTimeout(resolve, 10));
    process.off('unhandledRejection', onUnhandled);

    expect(seen).toEqual([]);
    expect(reg.size).toBe(0);
  });
});
