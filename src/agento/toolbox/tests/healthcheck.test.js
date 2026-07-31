import { describe, it, expect, vi, beforeEach } from 'vitest';

function sqlPoolRegistry() {
  return {
    createPoolHandle: ({ create }) => {
      let resource;
      return {
        use: async callback => callback(resource ??= await create()),
        invalidate: vi.fn(),
      };
    },
  };
}

describe('adapter healthchecks', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  describe('mysql', () => {
    it('returns ok for configured tool with working connection', async () => {
      const connection = {
        query: vi.fn().mockResolvedValue([[{ ok: 1 }]]),
        release: vi.fn(),
        destroy: vi.fn(),
      };
      vi.doMock('mysql2/promise', () => ({
        default: {
          createPool: () => ({
            getConnection: vi.fn().mockResolvedValue(connection),
          }),
        },
      }));
      vi.doMock('../log.js', () => ({ logToolboxMcp: vi.fn() }));
      vi.doMock('./sql-timeout.js', () => ({
        getSqlTimeoutMs: () => 300000,
      }));

      const { registerMysqlTools } = await import('../adapters/mysql.js');
      const server = { tool: vi.fn() };
      const tools = [
        { name: 'mysql_test', description: 'Test DB', config: { host: 'db', port: '3306', user: 'u', pass: 'p', database: 'd' } },
      ];

      const { names, healthcheck } = registerMysqlTools(server, tools, { sqlPoolRegistry: sqlPoolRegistry() });
      expect(names).toEqual(['mysql_test']);

      const results = await healthcheck();
      expect(results).toHaveLength(1);
      expect(results[0]).toMatchObject({ tool: 'mysql_test', status: 'ok' });
      expect(results[0].ms).toBeTypeOf('number');
    });

    it('returns skip for unconfigured tool', async () => {
      vi.doMock('mysql2/promise', () => ({ default: { createPool: vi.fn() } }));
      vi.doMock('../log.js', () => ({ logToolboxMcp: vi.fn() }));
      vi.doMock('./sql-timeout.js', () => ({
        getSqlTimeoutMs: () => 300000,
      }));

      const { registerMysqlTools } = await import('../adapters/mysql.js');
      const server = { tool: vi.fn() };
      const tools = [
        { name: 'mysql_nocfg', description: 'No config', config: { host: null, pass: null } },
      ];

      const { healthcheck } = registerMysqlTools(server, tools, { sqlPoolRegistry: sqlPoolRegistry() });
      const results = await healthcheck();
      expect(results[0]).toMatchObject({ tool: 'mysql_nocfg', status: 'skip' });
    });

    it('returns fail when connection throws', async () => {
      const connection = {
        query: vi.fn().mockRejectedValue(new Error('ECONNREFUSED')),
        release: vi.fn(),
        destroy: vi.fn(),
      };
      vi.doMock('mysql2/promise', () => ({
        default: {
          createPool: () => ({
            getConnection: vi.fn().mockResolvedValue(connection),
          }),
        },
      }));
      vi.doMock('../log.js', () => ({ logToolboxMcp: vi.fn() }));
      vi.doMock('./sql-timeout.js', () => ({
        getSqlTimeoutMs: () => 300000,
      }));

      const { registerMysqlTools } = await import('../adapters/mysql.js');
      const server = { tool: vi.fn() };
      const tools = [
        { name: 'mysql_broken', description: 'Broken', config: { host: 'db', pass: 'p' } },
      ];

      const { healthcheck } = registerMysqlTools(server, tools, { sqlPoolRegistry: sqlPoolRegistry() });
      const results = await healthcheck();
      expect(results[0]).toMatchObject({ tool: 'mysql_broken', status: 'fail', error: 'ECONNREFUSED' });
      expect(connection.release).toHaveBeenCalledOnce();
    });

    it('destroys the healthcheck connection when its deadline expires', async () => {
      let rejectQuery;
      const connection = {
        query: vi.fn(() => new Promise((_, reject) => { rejectQuery = reject; })),
        release: vi.fn(),
        destroy: vi.fn(() => rejectQuery(new Error('cancelled'))),
      };
      vi.doMock('mysql2/promise', () => ({
        default: { createPool: () => ({ getConnection: vi.fn().mockResolvedValue(connection) }) },
      }));
      vi.doMock('../log.js', () => ({ logToolboxMcp: vi.fn() }));

      const { registerMysqlTools } = await import('../adapters/mysql.js');
      const { healthcheck } = registerMysqlTools({ tool: vi.fn() }, [
        { name: 'mysql_slow', description: 'Slow', config: { host: 'db', pass: 'p' } },
      ], { sqlPoolRegistry: sqlPoolRegistry() });
      const results = await healthcheck({ timeoutMs: 5 });
      expect(results[0]).toMatchObject({ tool: 'mysql_slow', status: 'fail' });
      expect(connection.query).toHaveBeenCalledOnce();
      expect(connection.destroy).toHaveBeenCalledOnce();
      expect(connection.release).not.toHaveBeenCalled();
    });
  });

  describe('mssql', () => {
    it('returns ok for working connection', async () => {
      const mockRequest = { query: vi.fn().mockResolvedValue({ recordset: [{ ok: 1 }] }) };
      vi.doMock('mssql', () => ({
        default: {
          ConnectionPool: vi.fn(() => ({
            connect: vi.fn().mockResolvedValue(),
            close: vi.fn().mockResolvedValue(),
            request: () => mockRequest,
          })),
        },
      }));
      vi.doMock('../log.js', () => ({ logToolboxMcp: vi.fn() }));
      vi.doMock('./sql-timeout.js', () => ({ getSqlTimeoutMs: () => 300000 }));

      const { registerMssqlTools } = await import('../adapters/mssql.js');
      const server = { tool: vi.fn() };
      const tools = [
        { name: 'mssql_test', description: 'Test', config: { host: 'db', port: '1433', user: 'u', pass: 'p', database: 'd' } },
      ];

      const { names, healthcheck } = registerMssqlTools(server, tools, { sqlPoolRegistry: sqlPoolRegistry() });
      expect(names).toEqual(['mssql_test']);

      const results = await healthcheck();
      expect(results[0]).toMatchObject({ tool: 'mssql_test', status: 'ok' });
    });

    it('returns skip for unconfigured tool', async () => {
      vi.doMock('mssql', () => ({ default: {} }));
      vi.doMock('../log.js', () => ({ logToolboxMcp: vi.fn() }));
      vi.doMock('./sql-timeout.js', () => ({ getSqlTimeoutMs: () => 300000 }));

      const { registerMssqlTools } = await import('../adapters/mssql.js');
      const server = { tool: vi.fn() };
      const tools = [
        { name: 'mssql_nocfg', description: 'No config', config: { host: null, pass: null } },
      ];

      const { healthcheck } = registerMssqlTools(server, tools, { sqlPoolRegistry: sqlPoolRegistry() });
      const results = await healthcheck();
      expect(results[0]).toMatchObject({ tool: 'mssql_nocfg', status: 'skip' });
    });

    it('cancels an active MSSQL request when aborted', async () => {
      let rejectQuery;
      const request = {
        timeout: undefined,
        query: vi.fn(() => new Promise((_, reject) => { rejectQuery = reject; })),
        cancel: vi.fn(() => rejectQuery(new Error('cancelled'))),
      };
      vi.doMock('mssql', () => ({
        default: {
          ConnectionPool: vi.fn(() => ({
            healthy: true,
            connect: vi.fn().mockResolvedValue(),
            close: vi.fn().mockResolvedValue(),
            request: () => request,
          })),
        },
      }));
      vi.doMock('../log.js', () => ({ logToolboxMcp: vi.fn() }));

      const { registerMssqlTools } = await import('../adapters/mssql.js');
      const { healthcheck } = registerMssqlTools({ tool: vi.fn() }, [
        { name: 'mssql_slow', description: 'Slow', config: { host: 'db', pass: 'p' } },
      ], { sqlPoolRegistry: sqlPoolRegistry() });
      const controller = new globalThis.AbortController();
      const pending = healthcheck({ signal: controller.signal, timeoutMs: 1_000 });
      await vi.waitFor(() => expect(request.query).toHaveBeenCalledOnce());
      controller.abort();

      const results = await pending;
      expect(results[0]).toMatchObject({ tool: 'mssql_slow', status: 'fail' });
      expect(request.cancel).toHaveBeenCalledOnce();
      expect(request.timeout).toBe(1_000);
    });
  });

  describe('opensearch', () => {
    it('returns ok for reachable host', async () => {
      vi.doMock('../log.js', () => ({ logToolboxMcp: vi.fn() }));
      const originalFetch = globalThis.fetch;
      globalThis.fetch = vi.fn().mockResolvedValue({ ok: true });

      const { registerOpensearchTools } = await import('../adapters/opensearch.js');
      const server = { tool: vi.fn() };
      const tools = [
        { name: 'os_test', description: 'Test', config: { host: 'http://os:9200', user: 'u', pass: 'p' } },
      ];

      const { names, healthcheck } = registerOpensearchTools(server, tools);
      expect(names).toEqual(['os_test']);

      const results = await healthcheck();
      expect(results[0]).toMatchObject({ tool: 'os_test', status: 'ok' });

      globalThis.fetch = originalFetch;
    });

    it('returns fail for HTTP error', async () => {
      vi.doMock('../log.js', () => ({ logToolboxMcp: vi.fn() }));
      const originalFetch = globalThis.fetch;
      globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: 503 });

      const { registerOpensearchTools } = await import('../adapters/opensearch.js');
      const server = { tool: vi.fn() };
      const tools = [
        { name: 'os_down', description: 'Down', config: { host: 'http://os:9200', user: 'u', pass: 'p' } },
      ];

      const { healthcheck } = registerOpensearchTools(server, tools);
      const results = await healthcheck();
      expect(results[0]).toMatchObject({ tool: 'os_down', status: 'fail', error: 'HTTP 503' });

      globalThis.fetch = originalFetch;
    });

    it('returns skip for unconfigured tool', async () => {
      vi.doMock('../log.js', () => ({ logToolboxMcp: vi.fn() }));

      const { registerOpensearchTools } = await import('../adapters/opensearch.js');
      const server = { tool: vi.fn() };
      const tools = [
        { name: 'os_nocfg', description: 'No config', config: { host: null, pass: null } },
      ];

      const { healthcheck } = registerOpensearchTools(server, tools);
      const results = await healthcheck();
      expect(results[0]).toMatchObject({ tool: 'os_nocfg', status: 'skip' });
    });
  });

  describe('index (registerAdapterTools)', () => {
    it('collects names and healthchecks from all adapters', async () => {
      const mockHealthcheck = vi.fn().mockResolvedValue([{ tool: 'mock', status: 'ok' }]);
      vi.doMock('../adapters/mysql.js', () => ({
        registerMysqlTools: vi.fn().mockReturnValue({ names: ['mysql_a'], healthcheck: mockHealthcheck }),
        registerMysqlRootTools: vi.fn().mockReturnValue({ names: ['mysql_root_a'], healthcheck: mockHealthcheck }),
      }));
      vi.doMock('../adapters/mssql.js', () => ({
        registerMssqlTools: vi.fn().mockReturnValue({ names: ['mssql_a'], healthcheck: mockHealthcheck }),
      }));
      vi.doMock('../adapters/opensearch.js', () => ({
        registerOpensearchTools: vi.fn().mockReturnValue({ names: ['os_a'], healthcheck: mockHealthcheck }),
      }));

      const { registerAdapterTools } = await import('../adapters/index.js');
      const server = {};
      const { names, healthchecks } = registerAdapterTools(server, [], new Set());
      expect(names).toEqual(['mysql_a', 'mysql_root_a', 'mssql_a', 'os_a']);
      expect(healthchecks).toHaveLength(4);
    });
  });
});
