import { afterEach, describe, expect, it, vi } from 'vitest';

// Every adapter must log through the session's agent_view-scoped logger when one is injected,
// so toolbox_mcp.log attributes tool invocations to a scope — not only to the `user` argument
// the LLM supplies. Without an injected logger they fall back to the process-wide MCP logger
// (interactive runs and tool-list sessions have no agent_view).

const AGENT = 'agent@example.com';

let registry = null;

afterEach(async () => {
  await registry?.closeAll();
  registry = null;
  vi.restoreAllMocks();
  vi.resetModules();
});

function serverWithTools() {
  const tools = new Map();
  return {
    tools,
    tool: (name, description, schema, handler) => tools.set(name, { name, handler }),
  };
}

function mockLog() {
  const calls = [];
  vi.doMock('../log.js', () => ({
    logToolboxMcp: vi.fn((tool, status, details) => { calls.push({ tool, status, details }); }),
    logToolboxRest: vi.fn(),
    logPublisher: vi.fn(),
  }));
  return calls;
}

function collector() {
  const calls = [];
  return { calls, log: (tool, status, details) => { calls.push({ tool, status, details }); } };
}

describe('mssql adapter scoped logging', () => {
  async function build({ log } = {}) {
    const processLogCalls = mockLog();
    const ConnectionPool = vi.fn(config => ({
      config,
      connect: vi.fn().mockResolvedValue(),
      close: vi.fn().mockResolvedValue(),
      request: () => ({ query: vi.fn().mockResolvedValue({ recordset: [{ ok: 1 }] }) }),
    }));
    vi.doMock('mssql', () => ({ default: { ConnectionPool } }));

    const { registerMssqlTools } = await import('../adapters/mssql.js');
    const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
    registry = new SqlPoolRegistry();
    const server = serverWithTools();
    registerMssqlTools(
      server,
      [{ name: 'mssql_bi', description: 'BI', config: { host: 'bi.test', user: 'r', pass: 'p', database: 'bi' } }],
      { sqlPoolRegistry: registry, log }
    );
    return { tool: server.tools.get('mssql_bi'), processLogCalls };
  }

  it('routes QUERY/OK to the injected session logger', async () => {
    const scoped = collector();
    const { tool, processLogCalls } = await build({ log: scoped.log });
    await tool.handler({ user: AGENT, query: 'SELECT 1' });

    expect(scoped.calls.map(c => c.status)).toEqual(['QUERY', 'OK']);
    expect(scoped.calls[0].details).toContain(`user=${AGENT}`);
    expect(processLogCalls).toEqual([]);
  });

  it('routes BLOCKED to the injected session logger', async () => {
    const scoped = collector();
    const { tool, processLogCalls } = await build({ log: scoped.log });
    const result = await tool.handler({ user: AGENT, query: 'DELETE FROM report' });

    expect(result.isError).toBe(true);
    expect(scoped.calls.map(c => c.status)).toEqual(['BLOCKED']);
    expect(processLogCalls).toEqual([]);
  });

  it('falls back to the process logger when no session logger is injected', async () => {
    const { tool, processLogCalls } = await build();
    await tool.handler({ user: AGENT, query: 'SELECT 1' });

    expect(processLogCalls.map(c => c.status)).toEqual(['QUERY', 'OK']);
  });
});

describe('opensearch adapter scoped logging', () => {
  async function build({ log } = {}) {
    const processLogCalls = mockLog();
    const { registerOpensearchTools } = await import('../adapters/opensearch.js');
    const server = serverWithTools();
    registerOpensearchTools(
      server,
      [{ name: 'os_products', description: 'Products', config: { host: 'http://os.test:9200', user: 'u', pass: 'p' } }],
      { log }
    );
    return { tool: server.tools.get('os_products'), processLogCalls };
  }

  it('routes QUERY/OK to the injected session logger', async () => {
    const scoped = collector();
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ hits: { hits: [] } }),
    });
    try {
      const { tool, processLogCalls } = await build({ log: scoped.log });
      await tool.handler({ user: AGENT, index: 'products', query: '{"query":{"match_all":{}}}' });

      expect(scoped.calls.map(c => c.status)).toContain('QUERY');
      expect(scoped.calls[0].details).toContain(`user=${AGENT}`);
      expect(processLogCalls).toEqual([]);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('falls back to the process logger when no session logger is injected', async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ hits: { hits: [] } }),
    });
    try {
      const { tool, processLogCalls } = await build();
      await tool.handler({ user: AGENT, index: 'products', query: '{"query":{"match_all":{}}}' });

      expect(processLogCalls.map(c => c.status)).toContain('QUERY');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
