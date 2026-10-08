import { afterEach, describe, expect, it, vi } from 'vitest';
import { withConnections } from './fixtures/mysql-pool.js';

let registry;

afterEach(async () => {
  await registry?.closeAll();
  registry = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.resetModules();
});

function serverWithHandlers() {
  const handlers = new Map();
  return {
    handlers,
    tool: (name, _description, _schema, handler) => handlers.set(name, handler),
  };
}

async function loadRegistry() {
  const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
  registry = new SqlPoolRegistry();
  return registry;
}

describe('SQL pool registry', () => {
  it('isolates MSSQL pools by tool and resolved configuration', async () => {
    const pools = [];
    const ConnectionPool = vi.fn(config => {
      const pool = {
        config,
        healthy: true,
        connect: vi.fn().mockResolvedValue(),
        close: vi.fn().mockResolvedValue(),
        request: () => ({
          query: vi.fn().mockResolvedValue({
            recordset: [{ database: config.database, user: config.user }],
          }),
        }),
      };
      pools.push(pool);
      return pool;
    });
    vi.doMock('mssql', () => ({ default: { ConnectionPool } }));

    const { registerMssqlTools } = await import('../adapters/mssql.js');
    await loadRegistry();
    const server = serverWithHandlers();
    registerMssqlTools(server, [
      { name: 'mssql_analytics', description: 'Analytics', config: { host: 'analytics-db.test', user: 'analytics_reader', pass: 'fixture-pass-a', database: 'analytics' } },
      { name: 'mssql_erp', description: 'ERP', config: { host: 'erp-db.test', user: 'erp_reader', pass: 'fixture-pass-b', database: 'erp' } },
    ], { sqlPoolRegistry: registry });

    const [bi, nav] = await Promise.all([
      server.handlers.get('mssql_analytics')({ user: 'agent@example.com', query: 'SELECT DB_NAME()' }),
      server.handlers.get('mssql_erp')({ user: 'agent@example.com', query: 'SELECT DB_NAME()' }),
    ]);

    expect(ConnectionPool).toHaveBeenCalledTimes(2);
    expect(pools.map(pool => [pool.config.server, pool.config.user, pool.config.database])).toEqual([
      ['analytics-db.test', 'analytics_reader', 'analytics'],
      ['erp-db.test', 'erp_reader', 'erp'],
    ]);
    expect(pools.map(pool => pool.config.pool.max)).toEqual([10, 10]);
    expect(JSON.parse(bi.content[0].text)).toEqual([{ database: 'analytics', user: 'analytics_reader' }]);
    expect(JSON.parse(nav.content[0].text)).toEqual([{ database: 'erp', user: 'erp_reader' }]);
  });

  it('shares one MSSQL pool for concurrent sessions of the same tool and config', async () => {
    let resolveConnect;
    const connect = vi.fn(() => new Promise(resolve => { resolveConnect = resolve; }));
    const pool = {
      healthy: true,
      connect,
      close: vi.fn().mockResolvedValue(),
      request: () => ({ query: vi.fn().mockResolvedValue({ recordset: [] }) }),
    };
    const ConnectionPool = vi.fn(() => pool);
    vi.doMock('mssql', () => ({ default: { ConnectionPool } }));

    const { registerMssqlTools } = await import('../adapters/mssql.js');
    await loadRegistry();
    const firstServer = serverWithHandlers();
    const secondServer = serverWithHandlers();
    const tool = { name: 'mssql_bi', description: 'BI', config: { host: 'db', user: 'reader', pass: 'secret', database: 'warehouse' } };
    registerMssqlTools(firstServer, [tool], { sqlPoolRegistry: registry });
    registerMssqlTools(secondServer, [tool], { sqlPoolRegistry: registry });

    const first = firstServer.handlers.get('mssql_bi')({ user: 'agent@example.com', query: 'SELECT 1' });
    const second = secondServer.handlers.get('mssql_bi')({ user: 'agent@example.com', query: 'SELECT 1' });
    await vi.waitFor(() => expect(ConnectionPool).toHaveBeenCalledTimes(1));
    resolveConnect();
    await Promise.all([first, second]);

    expect(connect).toHaveBeenCalledTimes(1);
  });

  it('shares MySQL pools across sessions but creates a new one for changed config', async () => {
    const pools = [];
    const createPool = vi.fn(config => {
      const pool = withConnections({
        config,
        end: vi.fn().mockResolvedValue(),
        query: vi.fn().mockResolvedValue([[{ database: config.database }]]),
      });
      pools.push(pool);
      return pool;
    });
    vi.doMock('mysql2/promise', () => ({ default: { createPool } }));

    const { registerMysqlTools } = await import('../adapters/mysql.js');
    await loadRegistry();
    const firstServer = serverWithHandlers();
    const secondServer = serverWithHandlers();
    const changedConfigServer = serverWithHandlers();
    const base = {
      name: 'mysql_reporting',
      description: 'Reporting',
      config: {
        host: 'db',
        user: 'reader',
        pass: 'secret',
        database: 'reporting',
        client_connection_pool_max_per_tool: '3',
      },
    };
    registerMysqlTools(firstServer, [base], { sqlPoolRegistry: registry });
    registerMysqlTools(secondServer, [{ ...base, config: { ...base.config } }], { sqlPoolRegistry: registry });
    registerMysqlTools(changedConfigServer, [{ ...base, config: { ...base.config, database: 'reporting_archive' } }], { sqlPoolRegistry: registry });

    await firstServer.handlers.get('mysql_reporting')({ user: 'agent@example.com', query: 'SELECT 1' });
    await secondServer.handlers.get('mysql_reporting')({ user: 'agent@example.com', query: 'SELECT 1' });
    await changedConfigServer.handlers.get('mysql_reporting')({ user: 'agent@example.com', query: 'SELECT 1' });

    expect(createPool).toHaveBeenCalledTimes(2);
    expect(pools.map(pool => pool.config.database)).toEqual(['reporting', 'reporting_archive']);
    expect(pools.map(pool => pool.config.connectionLimit)).toEqual([3, 3]);
    expect(pools.map(pool => pool.config.maxIdle)).toEqual([undefined, undefined]);
  });

  it('defaults to one 10-operation budget across tools targeting the same server', async () => {
    await loadRegistry();
    const started = [];
    const releases = [];
    const makeHandle = toolName => registry.createPoolHandle({
      adapter: 'mssql',
      toolName,
      config: { server: 'shared-db.test', database: toolName },
      server: { host: 'shared-db.test', port: 1433 },
      create: () => ({}),
      close: vi.fn().mockResolvedValue(),
    });
    const run = (handle, name) => handle.use(() => new Promise(resolve => {
      started.push(name);
      releases.push(resolve);
    }));

    const operations = Array.from({ length: 11 }, (_, index) => {
      const name = `bi_${index + 1}`;
      return run(makeHandle(name), name);
    });
    await vi.waitFor(() => expect(started).toHaveLength(10));
    expect(started).toEqual(Array.from({ length: 10 }, (_, index) => `bi_${index + 1}`));

    releases.shift()();
    await vi.waitFor(() => expect(started).toHaveLength(11));
    expect(started[10]).toBe('bi_11');

    for (const release of releases) release();
    await Promise.all(operations);
  });

  it('closes idle pools and closes remaining pools during shutdown', async () => {
    vi.useFakeTimers();
    const close = vi.fn().mockResolvedValue();
    await loadRegistry();
    const handle = registry.createPoolHandle({
      adapter: 'mysql',
      toolName: 'mysql_reporting',
      config: { host: 'db', password: 'secret' },
      create: () => ({}),
      close,
    });

    await handle.use(async () => {});
    await vi.advanceTimersByTimeAsync(30_000);
    expect(close).toHaveBeenCalledTimes(1);

    const secondClose = vi.fn().mockResolvedValue();
    const secondHandle = registry.createPoolHandle({
      adapter: 'mssql',
      toolName: 'mssql_reporting',
      config: { server: 'db', password: 'secret' },
      create: () => ({}),
      close: secondClose,
    });
    await secondHandle.use(async () => {});
    await registry.closeAll();
    expect(secondClose).toHaveBeenCalledTimes(1);
  });

  it('bounds the server queue and cancels a queued operation on timeout', async () => {
    const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
    registry = new SqlPoolRegistry({ serverQueueLimit: 1 });
    let releaseFirst;
    const handle = registry.createPoolHandle({
      adapter: 'mysql',
      toolName: 'mysql_reporting',
      config: { host: 'shared-db.test' },
      server: { host: 'shared-db.test', port: 3306 },
      serverConcurrencyBudget: 1,
      queueWaitTimeoutMs: 5,
      create: () => ({}),
      close: vi.fn().mockResolvedValue(),
    });

    const first = handle.use(() => new Promise(resolve => { releaseFirst = resolve; }));
    await vi.waitFor(() => expect(releaseFirst).toBeTypeOf('function'));
    const timedOut = handle.use(async () => {});
    await expect(handle.use(async () => {})).rejects.toMatchObject({ code: 'SQL_QUEUE_FULL' });
    await expect(timedOut).rejects.toMatchObject({ code: 'SQL_QUEUE_TIMEOUT' });

    const controller = new globalThis.AbortController();
    const aborted = handle.use(async () => {}, { signal: controller.signal });
    controller.abort();
    await expect(aborted).rejects.toMatchObject({ code: 'SQL_QUEUE_ABORTED' });

    releaseFirst();
    await first;
  });

  it('keeps failed pool closes tracked, logs them, and retries on closeAll', async () => {
    const log = vi.fn();
    const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
    registry = new SqlPoolRegistry({ log });
    const close = vi.fn()
      .mockRejectedValueOnce(new Error('close failed'))
      .mockResolvedValueOnce();
    const handle = registry.createPoolHandle({
      adapter: 'mssql',
      toolName: 'mssql_reporting',
      config: { server: 'db.test' },
      server: { host: 'db.test', port: 1433 },
      create: () => ({}),
      close,
    });
    await handle.use(async () => {});

    await registry.closeAll();
    expect(close).toHaveBeenCalledTimes(1);
    expect(log).toHaveBeenCalledWith(
      'mssql_reporting',
      'ERROR',
      'Failed to close mssql pool (Error)'
    );

    await registry.closeAll();
    expect(close).toHaveBeenCalledTimes(2);
  });

  it('bounds repeated failed closes to one tracked entry per pool key', async () => {
    const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
    registry = new SqlPoolRegistry({ closeRetryBaseMs: 60_000 });
    const close = vi.fn().mockRejectedValue(new Error('close failed'));
    const handle = registry.createPoolHandle({
      adapter: 'mysql',
      toolName: 'mysql_reporting',
      config: { host: 'db.test' },
      server: { host: 'db.test', port: 3306 },
      create: () => ({}),
      close,
    });

    for (let attempt = 1; attempt <= 3; attempt += 1) {
      await handle.use(async () => {});
      handle.invalidate();
      await vi.waitFor(() => expect(close).toHaveBeenCalledTimes(attempt));
      expect(registry.failedCloseEntries.size).toBe(1);
      expect(registry.allEntries.size).toBe(1);
    }

    close.mockResolvedValue();
    await registry.closeAll();
  });

  it('stops tracking a pool after the configured close retry limit', async () => {
    const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
    registry = new SqlPoolRegistry({ closeRetryBaseMs: 60_000, maxCloseAttempts: 2 });
    const close = vi.fn().mockRejectedValue(new Error('close failed'));
    const handle = registry.createPoolHandle({
      adapter: 'mssql',
      toolName: 'mssql_reporting',
      config: { server: 'db.test' },
      server: { host: 'db.test', port: 1433 },
      create: () => ({}),
      close,
    });
    await handle.use(async () => {});

    await registry.closeAll();
    expect(registry.allEntries.size).toBe(1);
    await registry.closeAll();
    expect(registry.allEntries.size).toBe(0);
    expect(registry.failedCloseEntries.size).toBe(0);
  });
});
