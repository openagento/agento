import { afterEach, describe, expect, it, vi } from 'vitest';

let registries = [];

afterEach(async () => {
  await Promise.all(registries.map(registry => registry.closeAll()));
  registries = [];
  vi.restoreAllMocks();
  vi.resetModules();
});

async function newRegistry() {
  const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
  const registry = new SqlPoolRegistry();
  registries.push(registry);
  return registry;
}

describe('getSqlTimeoutMs', () => {
  it('returns default 300 000 ms when not configured', async () => {
    const { getSqlTimeoutMs } = await import('../adapters/sql-timeout.js');
    expect(getSqlTimeoutMs()).toBe(300_000);
  });

  it('converts configured seconds to milliseconds', async () => {
    const { getSqlTimeoutMs } = await import('../adapters/sql-timeout.js');
    expect(getSqlTimeoutMs(10)).toBe(10_000);
    expect(getSqlTimeoutMs(0)).toBe(0);
  });

  it('falls back safely for an invalid value', async () => {
    const { getSqlTimeoutMs } = await import('../adapters/sql-timeout.js');
    expect(getSqlTimeoutMs('invalid')).toBe(300_000);
  });
});

describe('MySQL tool timeout', () => {
  async function buildMysqlTool(sqlTimeoutSeconds) {
    const mockQuery = vi.fn().mockResolvedValue([[{ ok: 1 }]]);
    const mockPool = { query: mockQuery, end: vi.fn().mockResolvedValue() };
    vi.doMock('mysql2/promise', () => ({
      default: { createPool: () => mockPool },
    }));

    let handler;
    const fakeServer = {
      tool: (_name, _desc, _schema, fn) => { handler = fn; },
    };

    const { registerMysqlTools } = await import('../adapters/mysql.js');
    registerMysqlTools(fakeServer, [{
      name: 'mysql_test',
      description: 'Test MySQL',
      config: { host: 'localhost', port: 3306, user: 'user', pass: 'secret', database: 'testdb' },
    }], { sqlTimeoutSeconds, sqlPoolRegistry: await newRegistry() });

    return { handler, mockQuery };
  }

  it('passes default timeout to pool.query when not configured', async () => {
    const { handler, mockQuery } = await buildMysqlTool(undefined);
    await handler({ user: 'test@example.com', query: 'SELECT 1' });
    expect(mockQuery).toHaveBeenCalledWith({ sql: 'SELECT 1', timeout: 300_000 });
  });

  it('passes the timeout captured during tool registration', async () => {
    const { handler, mockQuery } = await buildMysqlTool(5);
    await handler({ user: 'test@example.com', query: 'SELECT 1' });
    expect(mockQuery).toHaveBeenCalledWith({ sql: 'SELECT 1', timeout: 5_000 });
  });
});

describe('MSSQL tool timeout', () => {
  async function buildMssqlTool(sqlTimeoutSeconds) {
    const ConnectionPool = vi.fn(() => ({
      healthy: true,
      connect: vi.fn().mockResolvedValue(),
      close: vi.fn().mockResolvedValue(),
      request: () => ({ query: vi.fn().mockResolvedValue({ recordset: [{ ok: 1 }] }) }),
    }));
    vi.doMock('mssql', () => ({ default: { ConnectionPool } }));

    let handler;
    const fakeServer = {
      tool: (_name, _desc, _schema, fn) => { handler = fn; },
    };

    const { registerMssqlTools } = await import('../adapters/mssql.js');
    registerMssqlTools(fakeServer, [{
      name: 'mssql_test',
      description: 'Test MSSQL',
      config: { host: 'localhost', port: 1433, user: 'user', pass: 'secret', database: 'testdb' },
    }], { sqlTimeoutSeconds, sqlPoolRegistry: await newRegistry() });

    return { handler, ConnectionPool };
  }

  it('passes default requestTimeout to the connection pool when not configured', async () => {
    const { handler, ConnectionPool } = await buildMssqlTool(undefined);
    await handler({ user: 'test@example.com', query: 'SELECT 1' });
    expect(ConnectionPool).toHaveBeenCalledWith(expect.objectContaining({ requestTimeout: 300_000 }));
  });

  it('passes the configured timeout as requestTimeout to the connection pool', async () => {
    const { handler, ConnectionPool } = await buildMssqlTool(5);
    await handler({ user: 'test@example.com', query: 'SELECT 1' });
    expect(ConnectionPool).toHaveBeenCalledWith(expect.objectContaining({ requestTimeout: 5_000 }));
  });

  it('keeps scoped timeouts isolated after another session registers', async () => {
    const requests = [];
    const ConnectionPool = vi.fn(() => ({
      healthy: true,
      connect: vi.fn().mockResolvedValue(),
      close: vi.fn().mockResolvedValue(),
      request: () => {
        const request = {
          query: vi.fn().mockResolvedValue({ recordset: [{ ok: 1 }] }),
        };
        requests.push(request);
        return request;
      },
    }));
    vi.doMock('mssql', () => ({ default: { ConnectionPool } }));

    const registry = await newRegistry();
    const { registerMssqlTools } = await import('../adapters/mssql.js');
    const handlers = [];
    const makeServer = () => ({
      tool: (_name, _desc, _schema, handler) => handlers.push(handler),
    });
    const tool = {
      name: 'mssql_test',
      description: 'Test MSSQL',
      config: { host: 'localhost', port: 1433, user: 'user', pass: 'secret', database: 'testdb' },
    };

    registerMssqlTools(makeServer(), [tool], {
      sqlTimeoutSeconds: 5,
      sqlPoolRegistry: registry,
    });
    registerMssqlTools(makeServer(), [tool], {
      sqlTimeoutSeconds: 300,
      sqlPoolRegistry: registry,
    });

    await handlers[0]({ user: 'test@example.com', query: 'SELECT 1' });
    await handlers[1]({ user: 'test@example.com', query: 'SELECT 1' });
    expect(requests).toHaveLength(2);
    expect(ConnectionPool.mock.calls.map(([cfg]) => cfg.requestTimeout)).toEqual([5_000, 300_000]);
  });
});
