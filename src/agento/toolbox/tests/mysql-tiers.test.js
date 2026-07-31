import { afterEach, describe, expect, it, vi } from 'vitest';

const CONFIG = { host: 'db.test', port: '3306', user: 'sandbox', pass: 'secret', database: 'sandbox' };
const AGENT = 'agent@example.com';
const RO_BLOCK_MESSAGE = 'Error: Only SELECT, SHOW, DESCRIBE, and EXPLAIN queries are allowed.';

// The read-only corpus is a regression fixture: `type: "mysql"` must keep making
// byte-for-byte the same allow/block decisions after the tier refactor.
const RO_ALLOWED = [
  'SELECT * FROM orders',
  'SELECT * FROM orders;',
  'SHOW TABLES',
  'SHOW CREATE TABLE orders',
  'DESCRIBE orders',
  'EXPLAIN SELECT id FROM orders',
  'WITH t AS (SELECT 1 AS id) SELECT * FROM t',
  'SELECT 1 # trailing comment',
  "SELECT 'DELETE FROM orders' AS harmless_text",
];

const RO_BLOCKED = [
  'INSERT INTO orders (id) VALUES (1)',
  'UPDATE orders SET total = 0',
  'DELETE FROM orders',
  'TRUNCATE TABLE orders',
  'DROP TABLE orders',
  'CREATE TABLE staging (id INT)',
  'ALTER TABLE orders ADD COLUMN note TEXT',
  'REPLACE INTO orders (id) VALUES (1)',
  'CALL rebuild_index()',
  'GRANT ALL ON sandbox.* TO reporter',
  'SET GLOBAL max_connections = 1',
  'USE other_db',
  'SELECT 1; DELETE FROM orders',
  "SELECT 1 INTO OUTFILE '/tmp/dump'",
  'WITH t AS (SELECT 1) UPDATE orders SET total = 1',
  'SELECT 1 /*!50000; DROP TABLE orders */',
];

const ROOT_WRITES = [
  'INSERT INTO orders (id) VALUES (1)',
  'UPDATE orders SET total = 0',
  'DELETE FROM orders',
  'TRUNCATE TABLE orders',
  'DROP TABLE orders',
  'CREATE TABLE staging (id INT)',
  'ALTER TABLE orders ADD COLUMN note TEXT',
  'REPLACE INTO orders (id) VALUES (1)',
  'CALL rebuild_index()',
  'SET GLOBAL max_connections = 1',
  'LOAD DATA INFILE \'/tmp/in.csv\' INTO TABLE orders',
];

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
    tool: (name, description, schema, handler, options) => {
      tools.set(name, { name, description, schema, handler, options });
    },
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

/**
 * Fake mysql2 driver. `rejectStacked` emulates a server running with the driver
 * default `multipleStatements: false`: a stacked batch arrives as ONE statement
 * and is rejected wholesale instead of executing statement by statement.
 */
function mockDriver({ rejectStacked = false, queryError = null } = {}) {
  const created = [];
  const executed = [];
  const createPool = vi.fn(config => {
    created.push(config);
    return {
      end: vi.fn().mockResolvedValue(),
      query: vi.fn(async request => {
        executed.push(request.sql);
        if (queryError) throw new Error(queryError);
        if (rejectStacked && /;\s*\S/.test(request.sql)) {
          const err = new Error("You have an error in your SQL syntax near '; DROP TABLE orders'");
          err.code = 'ER_PARSE_ERROR';
          throw err;
        }
        return [[{ affectedRows: 1 }]];
      }),
    };
  });
  vi.doMock('mysql2/promise', () => ({ default: { createPool } }));
  return { created, executed };
}

async function buildTiers(driverOptions = {}) {
  const driver = mockDriver(driverOptions);
  const logCalls = mockLog();

  const { registerMysqlTools, registerMysqlRootTools } = await import('../adapters/mysql.js');
  const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
  registry = new SqlPoolRegistry();

  const server = serverWithTools();
  const readOnly = registerMysqlTools(
    server,
    [{ name: 'mysql_reporting', description: 'Reporting MySQL.', config: { ...CONFIG } }],
    { sqlPoolRegistry: registry }
  );
  const root = registerMysqlRootTools(
    server,
    [{ name: 'mysql_sandbox_root', description: 'Agent sandbox MySQL.', config: { ...CONFIG } }],
    { sqlPoolRegistry: registry }
  );

  return {
    server,
    driver,
    logCalls,
    readOnly,
    root,
    ro: server.tools.get('mysql_reporting'),
    rw: server.tools.get('mysql_sandbox_root'),
  };
}

describe('mysql tier (type: "mysql") — read-only regression corpus', () => {
  it.each(RO_ALLOWED)('allows and executes: %s', async query => {
    const { ro, driver, logCalls } = await buildTiers();
    const result = await ro.handler({ user: AGENT, query });

    expect(result.isError).toBeUndefined();
    expect(driver.executed).toEqual([query]);
    expect(logCalls.some(c => c.status === 'BLOCKED')).toBe(false);
  });

  it.each(RO_BLOCKED)('blocks without touching the driver: %s', async query => {
    const { ro, driver, logCalls } = await buildTiers();
    const result = await ro.handler({ user: AGENT, query });

    expect(result.isError).toBe(true);
    expect(result.content[0].text).toBe(RO_BLOCK_MESSAGE);
    expect(driver.executed).toEqual([]);
    const blocked = logCalls.find(c => c.status === 'BLOCKED');
    expect(blocked.details).toContain(`user=${AGENT}`);
    expect(blocked.details).toContain('type=mysql');
  });

  it('keeps the declared description and the SELECT-only query contract', async () => {
    const { ro } = await buildTiers();

    expect(ro.description).toBe('Reporting MySQL.');
    expect(ro.schema.query.description).toBe('SQL query to execute (SELECT only)');
    expect(ro.options).toEqual({ resultStrategy: 'rows' });
  });

  it('logs the tier alongside the agent identity', async () => {
    const { ro, logCalls } = await buildTiers();
    await ro.handler({ user: AGENT, query: 'SELECT 1' });

    const query = logCalls.find(c => c.status === 'QUERY');
    const ok = logCalls.find(c => c.status === 'OK');
    expect(query.details).toContain(`user=${AGENT}`);
    expect(query.details).toContain('type=mysql |');
    expect(query.details).toContain('SELECT 1');
    expect(ok.details).toContain('type=mysql');
    expect(ok.details).not.toContain('mysql_root');
  });

  it('uses the "mysql" pool adapter label', async () => {
    const { ro } = await buildTiers();
    await ro.handler({ user: AGENT, query: 'SELECT 1' });

    expect([...registry.allEntries].map(e => e.adapter)).toEqual(['mysql']);
  });
});

describe('mysql_root tier (type: "mysql_root") — full read/write', () => {
  it.each(ROOT_WRITES)('executes write statement as-is: %s', async query => {
    const { rw, driver, logCalls } = await buildTiers();
    const result = await rw.handler({ user: AGENT, query });

    expect(result.isError).toBeUndefined();
    expect(driver.executed).toEqual([query]);
    expect(logCalls.some(c => c.status === 'BLOCKED')).toBe(false);
  });

  it('still executes ordinary reads', async () => {
    const { rw, driver } = await buildTiers();
    const result = await rw.handler({ user: AGENT, query: 'SELECT * FROM orders' });

    expect(result.isError).toBeUndefined();
    expect(JSON.parse(result.content[0].text)).toEqual([{ affectedRows: 1 }]);
    expect(driver.executed).toEqual(['SELECT * FROM orders']);
  });

  it('never emits the read-only block message', async () => {
    const { rw } = await buildTiers();
    const result = await rw.handler({ user: AGENT, query: 'DROP TABLE orders' });

    expect(result.content[0].text).not.toContain(RO_BLOCK_MESSAGE);
  });

  it('declares a full-access description and an any-SQL query contract', async () => {
    const { rw } = await buildTiers();

    expect(rw.description).toContain('Agent sandbox MySQL.');
    expect(rw.description).toContain('FULL ACCESS');
    // A timed-out write may still commit server-side, so the agent must not blind-retry.
    expect(rw.description).toMatch(/timeout/i);
    expect(rw.description).toMatch(/verify/i);
    expect(rw.schema.query.description).not.toContain('SELECT only');
    expect(rw.schema.query.description).toMatch(/any sql/i);
    expect(rw.options).toEqual({ resultStrategy: 'rows' });
  });

  it('logs the root tier on QUERY and OK lines', async () => {
    const { rw, logCalls } = await buildTiers();
    await rw.handler({ user: AGENT, query: 'DELETE FROM orders' });

    const query = logCalls.find(c => c.status === 'QUERY');
    const ok = logCalls.find(c => c.status === 'OK');
    expect(query.details).toContain(`user=${AGENT}`);
    expect(query.details).toContain('type=mysql_root');
    expect(query.details).toContain('DELETE FROM orders');
    expect(ok.details).toContain('type=mysql_root');
  });

  it('logs the root tier on ERROR lines and surfaces driver failures', async () => {
    const { rw, logCalls } = await buildTiers({ queryError: 'Table \'orders\' doesn\'t exist' });
    const result = await rw.handler({ user: AGENT, query: 'DELETE FROM orders' });

    expect(result.isError).toBe(true);
    expect(result.content[0].text).toContain('Query error:');
    const error = logCalls.find(c => c.status === 'ERROR');
    expect(error.details).toContain('type=mysql_root');
  });

  it('uses a distinct "mysql_root" pool adapter label', async () => {
    const { ro, rw } = await buildTiers();
    await ro.handler({ user: AGENT, query: 'SELECT 1' });
    await rw.handler({ user: AGENT, query: 'DELETE FROM orders' });

    expect([...registry.allEntries].map(e => e.adapter).sort()).toEqual(['mysql', 'mysql_root']);
  });

  it('reports not-configured instead of connecting when host/pass are missing', async () => {
    mockDriver();
    const logCalls = mockLog();
    const { registerMysqlRootTools } = await import('../adapters/mysql.js');
    const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
    registry = new SqlPoolRegistry();
    const server = serverWithTools();
    const { names, healthcheck } = registerMysqlRootTools(
      server,
      [{ name: 'mysql_sandbox_root', description: 'Sandbox', config: { database: 'sandbox' } }],
      { sqlPoolRegistry: registry }
    );

    const result = await server.tools.get('mysql_sandbox_root').handler({ user: AGENT, query: 'DROP TABLE orders' });

    expect(names).toEqual(['mysql_sandbox_root']);
    expect(result.isError).toBe(true);
    expect(result.content[0].text).toContain('not configured');
    expect(logCalls.find(c => c.status === 'ERROR').details).toContain('type=mysql_root');
    await expect(healthcheck()).resolves.toEqual([
      { tool: 'mysql_sandbox_root', status: 'skip', error: 'not configured' },
    ]);
  });

  it('requires the process-owned sqlPoolRegistry', async () => {
    mockDriver();
    mockLog();
    const { registerMysqlRootTools } = await import('../adapters/mysql.js');
    expect(() => registerMysqlRootTools(serverWithTools(), [
      { name: 'mysql_sandbox_root', description: 'Sandbox', config: { ...CONFIG } },
    ], {})).toThrow(/sqlPoolRegistry/);
  });
});

describe('agent_view-scoped audit logging', () => {
  async function buildWithScopedLog(tier) {
    const driver = mockDriver();
    const processLogCalls = mockLog();
    const scopedCalls = [];
    const scopedLog = (tool, status, details) => { scopedCalls.push({ tool, status, details }); };

    const { registerMysqlTools, registerMysqlRootTools } = await import('../adapters/mysql.js');
    const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
    registry = new SqlPoolRegistry();
    const server = serverWithTools();
    const register = tier === 'root' ? registerMysqlRootTools : registerMysqlTools;
    const name = tier === 'root' ? 'mysql_tool_root' : 'mysql_tool';
    register(server, [{ name, description: 'Tool', config: { ...CONFIG } }], {
      sqlPoolRegistry: registry,
      log: scopedLog,
    });

    return { tool: server.tools.get(name), driver, processLogCalls, scopedCalls };
  }

  it('sends mysql_root QUERY/OK to the injected session logger, not the process logger', async () => {
    const { tool, processLogCalls, scopedCalls } = await buildWithScopedLog('root');
    await tool.handler({ user: AGENT, query: 'DELETE FROM orders' });

    expect(scopedCalls.map(c => c.status)).toEqual(['QUERY', 'OK']);
    expect(scopedCalls[0].details).toContain('type=mysql_root');
    expect(scopedCalls[0].details).toContain('DELETE FROM orders');
    expect(processLogCalls).toEqual([]);
  });

  it('sends mysql_root driver failures to the injected session logger', async () => {
    const driver = mockDriver({ queryError: 'Deadlock found' });
    const processLogCalls = mockLog();
    const scopedCalls = [];
    const { registerMysqlRootTools } = await import('../adapters/mysql.js');
    const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
    registry = new SqlPoolRegistry();
    const server = serverWithTools();
    registerMysqlRootTools(server, [{ name: 'mysql_tool_root', description: 'Tool', config: { ...CONFIG } }], {
      sqlPoolRegistry: registry,
      log: (tool, status, details) => { scopedCalls.push({ tool, status, details }); },
    });

    await server.tools.get('mysql_tool_root').handler({ user: AGENT, query: 'DELETE FROM orders' });

    expect(scopedCalls.find(c => c.status === 'ERROR').details).toContain('type=mysql_root');
    expect(processLogCalls).toEqual([]);
    expect(driver.executed).toEqual(['DELETE FROM orders']);
  });

  it('sends read-only BLOCKED decisions to the injected session logger', async () => {
    const { tool, processLogCalls, scopedCalls } = await buildWithScopedLog('read-only');
    await tool.handler({ user: AGENT, query: 'DELETE FROM orders' });

    expect(scopedCalls.map(c => c.status)).toEqual(['BLOCKED']);
    expect(processLogCalls).toEqual([]);
  });

  it('falls back to the process logger when no session logger is injected', async () => {
    const { rw, logCalls } = await buildTiers();
    await rw.handler({ user: AGENT, query: 'DELETE FROM orders' });

    expect(logCalls.map(c => c.status)).toEqual(['QUERY', 'OK']);
  });
});

describe('mysql_root name marker (capability visible in the tool name)', () => {
  async function registerRoot(toolName) {
    const driver = mockDriver();
    const logCalls = mockLog();
    const { registerMysqlRootTools } = await import('../adapters/mysql.js');
    const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
    registry = new SqlPoolRegistry();
    const server = serverWithTools();
    const result = registerMysqlRootTools(
      server,
      [{ name: toolName, description: 'Sandbox', config: { ...CONFIG } }],
      { sqlPoolRegistry: registry }
    );
    return { server, result, driver, logCalls };
  }

  it('refuses to register a full-access tool whose name does not end in _root', async () => {
    const { server, result, logCalls } = await registerRoot('mysql_sandbox');

    expect(result.names).toEqual([]);
    expect(server.tools.has('mysql_sandbox')).toBe(false);
    const error = logCalls.find(c => c.status === 'ERROR');
    expect(error.tool).toBe('mysql_sandbox');
    expect(error.details).toContain('_root');
    await expect(result.healthcheck()).resolves.toEqual([]);
  });

  it('registers a correctly named full-access tool', async () => {
    const { server, result, logCalls } = await registerRoot('mysql_sandbox_root');

    expect(result.names).toEqual(['mysql_sandbox_root']);
    expect(server.tools.has('mysql_sandbox_root')).toBe(true);
    expect(logCalls.some(c => c.status === 'ERROR')).toBe(false);
  });

  it('does not impose the suffix on read-only tools', async () => {
    const { ro, rw } = await buildTiers();

    expect(ro.name).toBe('mysql_reporting');
    expect(rw.name).toBe('mysql_sandbox_root');
  });

  it('refuses a read-only tool that squats a reserved _root name', async () => {
    // Otherwise a read-only tool could take the name first, get enabled, and later be
    // escalated by editing only its type — keeping the name-keyed is_enabled grant.
    mockDriver();
    const logCalls = mockLog();
    const { registerMysqlTools } = await import('../adapters/mysql.js');
    const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
    registry = new SqlPoolRegistry();
    const server = serverWithTools();
    const result = registerMysqlTools(
      server,
      [{ name: 'customer_db_root', description: 'Reporting', config: { ...CONFIG } }],
      { sqlPoolRegistry: registry }
    );

    expect(result.names).toEqual([]);
    expect(server.tools.has('customer_db_root')).toBe(false);
    const error = logCalls.find(c => c.status === 'ERROR');
    expect(error.tool).toBe('customer_db_root');
    expect(error.details).toMatch(/reserved/i);
  });
});

describe('mysql_root multi-statement stacking', () => {
  it('does not enable multipleStatements on either tier pool', async () => {
    const { ro, rw, driver } = await buildTiers();
    await ro.handler({ user: AGENT, query: 'SELECT 1' });
    await rw.handler({ user: AGENT, query: 'SELECT 1' });

    expect(driver.created).toHaveLength(2);
    for (const config of driver.created) {
      expect(config.multipleStatements).toBeUndefined();
    }
  });

  it('hands a stacked batch to the driver as ONE statement and surfaces its rejection', async () => {
    const stacked = "INSERT INTO orders (id) VALUES (1); DROP TABLE orders";
    const { rw, driver } = await buildTiers({ rejectStacked: true });
    const result = await rw.handler({ user: AGENT, query: stacked });

    expect(driver.executed).toEqual([stacked]);
    expect(result.isError).toBe(true);
    expect(result.content[0].text).toContain('error in your SQL syntax');
  });
});

describe('registerAdapterTools tier dispatch', () => {
  async function dispatch(tools, types) {
    const driver = mockDriver();
    const logCalls = mockLog();
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const { registerAdapterTools } = await import('../adapters/index.js');
    const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
    registry = new SqlPoolRegistry();
    const server = serverWithTools();
    const { names } = registerAdapterTools(server, tools, new Set(types), {}, { sqlPoolRegistry: registry });

    return { server, names, driver, logCalls, errSpy };
  }

  it('routes type "mysql_root" to the root tier and type "mysql" to the read-only tier', async () => {
    const { server, names, driver, errSpy } = await dispatch(
      [
        { type: 'mysql', name: 'mysql_reporting', description: 'Reporting', config: { ...CONFIG } },
        { type: 'mysql_root', name: 'mysql_sandbox_root', description: 'Sandbox', config: { ...CONFIG } },
      ],
      ['mysql', 'mysql_root']
    );

    expect(names).toEqual(expect.arrayContaining(['mysql_reporting', 'mysql_sandbox_root']));

    const blocked = await server.tools.get('mysql_reporting').handler({ user: AGENT, query: 'INSERT INTO orders (id) VALUES (1)' });
    expect(blocked.isError).toBe(true);
    expect(blocked.content[0].text).toBe(RO_BLOCK_MESSAGE);

    const allowed = await server.tools.get('mysql_sandbox_root').handler({ user: AGENT, query: 'INSERT INTO orders (id) VALUES (1)' });
    expect(allowed.isError).toBeUndefined();
    expect(driver.executed).toEqual(['INSERT INTO orders (id) VALUES (1)']);

    const warned = errSpy.mock.calls.some(c => String(c[0]).includes('No adapter for tool type'));
    expect(warned).toBe(false);
  });

  it('forwards the session logger to the tier registrars', async () => {
    const scopedCalls = [];
    const driver = mockDriver();
    const processLogCalls = mockLog();

    const { registerAdapterTools } = await import('../adapters/index.js');
    const { SqlPoolRegistry } = await import('../adapters/sql-pool-registry.js');
    registry = new SqlPoolRegistry();
    const server = serverWithTools();
    registerAdapterTools(
      server,
      [{ type: 'mysql_root', name: 'mysql_sandbox_root', description: 'Sandbox', config: { ...CONFIG } }],
      new Set(['mysql_root']),
      {},
      { sqlPoolRegistry: registry, log: (tool, status, details) => { scopedCalls.push({ tool, status, details }); } }
    );

    await server.tools.get('mysql_sandbox_root').handler({ user: AGENT, query: 'DELETE FROM orders' });

    expect(scopedCalls.map(c => c.status)).toEqual(['QUERY', 'OK']);
    expect(processLogCalls).toEqual([]);
    expect(driver.executed).toEqual(['DELETE FROM orders']);
  });

  it('still warns for a genuinely unknown tool type', async () => {
    const { errSpy } = await dispatch([], ['mysql_sandbox_typo']);
    const warned = errSpy.mock.calls.some(c => String(c[0]).includes('No adapter for tool type "mysql_sandbox_typo"'));
    expect(warned).toBe(true);
  });
});
