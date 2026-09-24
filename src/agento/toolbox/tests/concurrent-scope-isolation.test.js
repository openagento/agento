import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createRequireCapability, createVerifier, tokenHash } from '../capability.js';
import { capabilityRow } from './capability-rows.js';

// The plan requires two agent runs to hold MCP sessions at the same time without either
// seeing the other's scope. Every other capability test drives ONE request at a time, so a
// scope kept in a module-level variable ("the view of the request being served") or a shared
// overrides object would pass all of them and still cross the two live sessions. These tests
// run both requests CONCURRENTLY, and force the second to finish its DB work first, so any
// last-write-wins state is what the assertion reads.

const res = () => {
  const r = { statusCode: null, body: null };
  r.status = (c) => { r.statusCode = c; return r; };
  r.json = (b) => { r.body = b; return r; };
  return r;
};
const request = (token) => ({ headers: { authorization: `Bearer ${token}` }, query: {}, body: {} });

describe('concurrent capability scopes', () => {
  it('gives each in-flight request its own scope when their lookups interleave', async () => {
    const rows = {
      'tok-a': capabilityRow('mcp_interactive', { agent_view_id: 7 }),
      'tok-b': capabilityRow('mcp_interactive', { agent_view_id: 9 }),
    };
    // A's row resolves LAST: if the middleware parked the claims anywhere shared, A would
    // finish holding B's view (9) rather than its own (7).
    const query = vi.fn(async (_sql, params) => {
      const entry = Object.entries(rows).find(([tok]) => params.includes(tokenHash(tok)));
      if (!entry) return [[]];
      if (entry[0] === 'tok-a') await new Promise((r) => setTimeout(r, 20));
      return [[entry[1]]];
    });

    const guard = createRequireCapability(
      createVerifier(query), { endpoint: 'mcp' }, () => {}
    );
    const reqA = request('tok-a');
    const reqB = request('tok-b');
    const run = (req) => new Promise((resolve) => guard(req, res(), resolve));
    await Promise.all([run(reqA), run(reqB)]);

    expect(reqA.capability.agent_view_id).toBe(7);
    expect(reqB.capability.agent_view_id).toBe(9);
  });
});

describe('concurrent scoped config loads', () => {
  let loadScopedDbOverridesStrict;

  beforeEach(async () => {
    vi.resetModules();
    // Every scoped load layers workspace + agent_view rows ON TOP of the global overrides
    // object. A cached or module-level global object would be MUTATED by both loads, so each
    // session would end up seeing whichever view wrote last.
    vi.doMock('../db.js', () => ({
      getCronPool: () => ({
        query: async (sql, params) => {
          if (sql.includes("scope = 'default'")) return [[{ path: 'jira/url', value: 'global', encrypted: 0 }]];
          if (sql.includes('FROM agent_view')) {
            const id = params[0];
            await new Promise((r) => setTimeout(r, id === 7 ? 20 : 0));
            return [[{ id, workspace_id: id * 10, label: `v${id}`, agent_view_code: `c${id}`, workspace_code: `w${id}` }]];
          }
          if (sql.includes("scope = 'agent_view'")) {
            return [[{ path: 'jira/url', value: `view-${params[0]}`, encrypted: 0 }]];
          }
          return [[]];
        },
      }),
    }));
    vi.doMock('../log.js', () => ({ logToolboxRest: vi.fn(), logToolboxMcp: vi.fn(), errorCategory: () => 'failed' }));
    ({ loadScopedDbOverridesStrict } = await import('../config-loader.js'));
  });

  afterEach(() => { vi.doUnmock('../db.js'); vi.doUnmock('../log.js'); vi.resetModules(); });

  it('never lets one view\'s overrides land in the other\'s result', async () => {
    const [a, b] = await Promise.all([
      loadScopedDbOverridesStrict(7),
      loadScopedDbOverridesStrict(9),
    ]);
    expect(a.overrides['jira/url'].value).toBe('view-7');
    expect(b.overrides['jira/url'].value).toBe('view-9');
    expect(a.agentViewMeta.id).toBe(7);
    expect(b.agentViewMeta.id).toBe(9);
    expect(a.overrides).not.toBe(b.overrides);
  });
});
