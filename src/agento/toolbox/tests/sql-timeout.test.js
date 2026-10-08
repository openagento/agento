import { afterEach, describe, expect, it, vi } from 'vitest';
import { withConnections } from './fixtures/mysql-pool.js';

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
    const mockPool = withConnections({ query: mockQuery, end: vi.fn().mockResolvedValue() });
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
  it('keeps scoped timeouts isolated after another session registers', async () => {
    const requests = [];
    const ConnectionPool = vi.fn(() => ({
      healthy: true,
      connect: vi.fn().mockResolvedValue(),
      close: vi.fn().mockResolvedValue(),
      request: () => {
        const request = {
          timeout: undefined,
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
    expect(requests.map(request => request.timeout)).toEqual([5_000, 300_000]);
    expect(ConnectionPool).toHaveBeenCalledTimes(1);
  });
});

describe('getSqlServerLimits', () => {
  it('keeps the statement timeout 5 s under the client timeout and defaults lock wait to 60 s', async () => {
    const { getSqlServerLimits } = await import('../adapters/sql-timeout.js');
    expect(getSqlServerLimits(300_000)).toEqual({ statementTimeoutMs: 295_000, lockWaitTimeoutSeconds: 60 });
    expect(getSqlServerLimits(3_000)).toEqual({ statementTimeoutMs: 1_000, lockWaitTimeoutSeconds: 60 });
    expect(getSqlServerLimits(0)).toEqual({ statementTimeoutMs: 0, lockWaitTimeoutSeconds: 60 });
  });

  it('uses configured values and ignores invalid ones', async () => {
    const { getSqlServerLimits } = await import('../adapters/sql-timeout.js');
    expect(getSqlServerLimits(300_000, { statementTimeoutSeconds: '30', lockWaitTimeoutSeconds: 5 }))
      .toEqual({ statementTimeoutMs: 30_000, lockWaitTimeoutSeconds: 5 });
    expect(getSqlServerLimits(300_000, { statementTimeoutSeconds: 'x', lockWaitTimeoutSeconds: 0 }))
      .toEqual({ statementTimeoutMs: 295_000, lockWaitTimeoutSeconds: 60 });
  });
});

describe('MySQL server-side limits and cancellation', () => {
  function driverError(message, fields) {
    return Object.assign(new Error(message), fields);
  }

  async function buildTool({ serverVersion = '8.0.36', queryError = null, killError = null, config = {} } = {}) {
    const mockQuery = vi.fn(async () => {
      if (queryError) throw queryError;
      return [[{ ok: 1 }]];
    });
    const mockPool = withConnections({ query: mockQuery, end: vi.fn().mockResolvedValue() }, { serverVersion });
    const killer = {
      query: vi.fn(async () => {
        if (killError) throw killError;
        return [{}];
      }),
      destroy: vi.fn(),
    };
    const createConnection = vi.fn().mockResolvedValue(killer);
    vi.doMock('mysql2/promise', () => ({
      default: { createPool: () => mockPool, createConnection },
    }));

    let handler;
    const { registerMysqlTools } = await import('../adapters/mysql.js');
    registerMysqlTools({ tool: (_name, _desc, _schema, fn) => { handler = fn; } }, [{
      name: 'mysql_test',
      description: 'Test MySQL',
      config: { host: 'localhost', port: 3306, user: 'user', pass: 'secret', database: 'testdb', ...config },
    }], { sqlTimeoutSeconds: 300, sqlPoolRegistry: await newRegistry() });

    const run = () => handler({ user: 'test@example.com', query: 'SELECT 1' });
    return { run, connection: mockPool.connection, createConnection, killer };
  }

  it('sets MariaDB max_statement_time and lock_wait_timeout once per connection', async () => {
    const { run, connection } = await buildTool({ serverVersion: '5.5.5-10.6.18-MariaDB-log' });
    await run();
    await run();
    const setCalls = connection.query.mock.calls.filter(([sql]) => String(sql).startsWith('SET SESSION'));
    expect(setCalls).toEqual([['SET SESSION max_statement_time = 295, lock_wait_timeout = 60']]);
  });

  it('sets MySQL max_execution_time in milliseconds', async () => {
    const { run, connection } = await buildTool({ serverVersion: '8.0.36' });
    await run();
    expect(connection.query).toHaveBeenCalledWith('SET SESSION max_execution_time = 295000, lock_wait_timeout = 60');
  });

  it('takes per-tool statement and lock wait limits from tool config', async () => {
    const { run, connection } = await buildTool({
      config: { statement_timeout_seconds: '20', lock_wait_timeout_seconds: '7' },
    });
    await run();
    expect(connection.query).toHaveBeenCalledWith('SET SESSION max_execution_time = 20000, lock_wait_timeout = 7');
  });

  it('kills the server-side statement and destroys the connection after a client timeout', async () => {
    const { run, connection, createConnection, killer } = await buildTool({
      queryError: driverError('Query inactivity timeout', { code: 'PROTOCOL_SEQUENCE_TIMEOUT' }),
    });
    const result = await run();

    expect(createConnection).toHaveBeenCalledWith(expect.objectContaining({ host: 'localhost', user: 'user' }));
    expect(createConnection.mock.calls[0][0]).not.toHaveProperty('connectionLimit');
    expect(killer.query).toHaveBeenCalledWith(expect.objectContaining({ sql: 'KILL QUERY ?', values: [42] }));
    expect(killer.destroy).toHaveBeenCalled();
    expect(connection.destroy).toHaveBeenCalled();
    expect(connection.release).not.toHaveBeenCalled();
    expect(result.isError).toBe(true);
    expect(result.content[0].text).toMatch(/^Query exceeded 300s and was cancelled on the server\. Retrying unchanged/);
  });

  it('tells the agent the statement may still run when KILL QUERY fails', async () => {
    const { run } = await buildTool({
      queryError: driverError('Query inactivity timeout', { code: 'PROTOCOL_SEQUENCE_TIMEOUT' }),
      killError: new Error('connect ETIMEDOUT'),
    });
    const result = await run();
    expect(result.content[0].text).toMatch(/could not be cancelled on the server.*Do not retry the same query/);
  });

  it('returns the lock message for a lock wait timeout and keeps the connection', async () => {
    const { run, connection, createConnection } = await buildTool({
      queryError: driverError('Lock wait timeout exceeded; try restarting transaction', { errno: 1205 }),
    });
    const result = await run();
    expect(result.content[0].text).toMatch(/^Query blocked by a lock on the server \(not a query bug\)\..*Do not retry/);
    expect(createConnection).not.toHaveBeenCalled();
    expect(connection.release).toHaveBeenCalled();
  });

  it.each([1969, 3024])('returns the statement timeout message for server error %i', async errno => {
    const { run } = await buildTool({
      queryError: driverError('Query execution was interrupted', { errno }),
    });
    const result = await run();
    expect(result.content[0].text).toMatch(/^Query exceeded 295s and was cancelled on the server\./);
  });

  it('keeps the raw driver message for other errors', async () => {
    const { run } = await buildTool({
      queryError: driverError("Table 'testdb.nope' doesn't exist", { errno: 1146 }),
    });
    const result = await run();
    expect(result.content[0].text).toBe("Query error: Table 'testdb.nope' doesn't exist");
  });
});

describe('MSSQL lock timeout and error text', () => {
  async function buildTool({ queryError = null, config = {} } = {}) {
    const requests = [];
    const ConnectionPool = vi.fn(() => ({
      healthy: true,
      connect: vi.fn().mockResolvedValue(),
      close: vi.fn().mockResolvedValue(),
      request: () => {
        const request = {
          query: vi.fn(async () => {
            if (queryError) throw queryError;
            return { recordset: [{ ok: 1 }] };
          }),
        };
        requests.push(request);
        return request;
      },
    }));
    vi.doMock('mssql', () => ({ default: { ConnectionPool } }));

    let handler;
    const { registerMssqlTools } = await import('../adapters/mssql.js');
    registerMssqlTools({ tool: (_name, _desc, _schema, fn) => { handler = fn; } }, [{
      name: 'mssql_test',
      description: 'Test MSSQL',
      config: { host: 'localhost', port: 1433, user: 'user', pass: 'secret', database: 'testdb', ...config },
    }], { sqlTimeoutSeconds: 300, sqlPoolRegistry: await newRegistry() });

    const run = () => handler({ user: 'test@example.com', query: 'SELECT 1' });
    return { run, requests };
  }

  it('prefixes every request with SET LOCK_TIMEOUT', async () => {
    const { run, requests } = await buildTool();
    await run();
    expect(requests[0].query).toHaveBeenCalledWith('SET LOCK_TIMEOUT 60000;\nSELECT 1');
  });

  it('uses the per-tool lock wait timeout', async () => {
    const { run, requests } = await buildTool({ config: { lock_wait_timeout_seconds: '3' } });
    await run();
    expect(requests[0].query).toHaveBeenCalledWith('SET LOCK_TIMEOUT 3000;\nSELECT 1');
  });

  it('returns the lock message for error 1222', async () => {
    const { run } = await buildTool({
      queryError: Object.assign(new Error('Lock request time out period exceeded.'), { number: 1222 }),
    });
    const result = await run();
    expect(result.content[0].text).toMatch(/^Query blocked by a lock on the server/);
  });

  it('returns the statement timeout message for a request timeout', async () => {
    const { run } = await buildTool({
      queryError: Object.assign(new Error('Timeout: Request failed to complete in 300000ms'), { code: 'ETIMEOUT' }),
    });
    const result = await run();
    expect(result.content[0].text).toMatch(/^Query exceeded 300s and was cancelled on the server\./);
  });
});

describe('tedious request timeout', () => {
  // mssql relies on tedious to cancel a timed-out request on the server: request.cancel() makes
  // the connection send a TDS ATTENTION packet (Connection#_cancelAfterRequestSent).
  it('cancels the request instead of only rejecting it', async () => {
    const { Connection } = await import('tedious');
    const connection = Object.create(Connection.prototype);
    const request = { cancel: vi.fn(), timeout: 5_000 };
    connection.request = request;
    connection.config = { options: {} };

    connection.requestTimeout();

    expect(request.cancel).toHaveBeenCalledTimes(1);
    expect(request.error.code).toBe('ETIMEOUT');
  });
});
