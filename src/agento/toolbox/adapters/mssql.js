import { z } from 'zod';
import sql from 'mssql';
import { logToolboxMcp as processLog } from '../log.js';
import { runCancellable } from '../cancellable-operation.js';
import { isReadOnlySql } from './sql-read-only.js';
import { getSqlTimeoutMs } from './sql-timeout.js';

const ALLOWED_KEYWORDS = ['SELECT', 'WITH'];

function createMssqlTool(server, toolName, description, config, options) {
  // The session logger carries the agent_view label/id, so tool invocations are attributable to
  // a scope — not only to the LLM-supplied `user`. Falls back to the process-wide MCP logger for
  // interactive runs and tool-list sessions with no agent_view.
  const log = options.log || processLog;
  const port = parseInt(config.port || '1433');
  const configuredPoolMax = Number.parseInt(config.client_connection_pool_max_per_tool, 10);
  const poolMax = Number.isInteger(configuredPoolMax) && configuredPoolMax > 0
    ? configuredPoolMax
    : options.clientConnectionPoolMaxPerTool;
  const mssqlConfig = {
    server: config.host,
    port,
    user: config.user,
    password: config.pass,
    database: config.database,
    options: { encrypt: true, trustServerCertificate: true },
    pool: { max: poolMax, min: 0, idleTimeoutMillis: 30000 },
  };

  const poolHandle = options.sqlPoolRegistry.createPoolHandle({
    adapter: 'mssql',
    toolName,
    config: mssqlConfig,
    server: { host: String(config.host).trim().toLowerCase(), port },
    serverConcurrencyBudget: options.serverConcurrencyBudget,
    queueWaitTimeoutMs: options.sqlTimeoutMs || 300_000,
    create: async () => {
      const pool = new sql.ConnectionPool(mssqlConfig);
      await pool.connect();
      return pool;
    },
    close: pool => pool.close(),
  });

  server.tool(
    toolName,
    description,
    {
      user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
      query: z.string().describe('SQL query to execute (SELECT only)'),
    },
    async ({ user, query }) => {
      if (!config.host || !config.pass) {
        log(toolName, 'ERROR', `user=${user} - not configured (missing host or pass)`);
        return {
          content: [{ type: 'text', text: `Error: ${toolName} not configured. Set host and pass via bin/agento config:set or ENV vars.` }],
          isError: true,
        };
      }

      if (!isReadOnlySql(query, ALLOWED_KEYWORDS, { dialect: 'mssql' })) {
        log(toolName, 'BLOCKED', `user=${user} non-readonly query: ${query.substring(0, 80)}`);
        return {
          content: [{ type: 'text', text: 'Error: Only SELECT and WITH queries are allowed.' }],
          isError: true,
        };
      }

      log(toolName, 'QUERY', `user=${user} | ${query}`);
      const start = Date.now();
      let pool;

      try {
        const result = await poolHandle.use(async p => {
          pool = p;
          const req = p.request();
          req.timeout = options.sqlTimeoutMs;
          return req.query(query);
        });
        const elapsed = Date.now() - start;
        const rows = result.recordset;

        log(toolName, 'OK', `user=${user} time=${elapsed}ms rows=${rows.length}`);

        const text = JSON.stringify(rows, null, 2);
        return { content: [{ type: 'text', text }] };
      } catch (err) {
        if (pool?.healthy === false) poolHandle.invalidate();
        log(toolName, 'ERROR', `user=${user} ${err.message}`);
        return {
          content: [{ type: 'text', text: `Query error: ${err.message}` }],
          isError: true,
        };
      }
    },
    { resultStrategy: 'rows' }
  );

  return poolHandle;
}

/**
 * Register MSSQL tools from pre-resolved tool configs.
 * @returns {{ names: string[], healthcheck: () => Promise<Array> }}
 */
export function registerMssqlTools(server, tools, options = {}) {
  if (tools.length > 0 && !options.sqlPoolRegistry) {
    throw new Error('MSSQL adapter requires sqlPoolRegistry');
  }
  const resolvedOptions = {
    clientConnectionPoolMaxPerTool: options.clientConnectionPoolMaxPerTool || 10,
    serverConcurrencyBudget: options.serverConcurrencyBudget || 10,
    sqlPoolRegistry: options.sqlPoolRegistry,
    sqlTimeoutMs: getSqlTimeoutMs(options.sqlTimeoutSeconds),
    log: options.log,
  };
  const registered = [];
  const poolRefs = [];

  for (const tool of tools) {
    const poolHandle = createMssqlTool(server, tool.name, tool.description, tool.config, resolvedOptions);
    registered.push(tool.name);
    poolRefs.push({ name: tool.name, poolHandle, config: tool.config });
  }

  async function healthcheck({ signal, timeoutMs = 10_000 } = {}) {
    const results = [];
    for (const { name, poolHandle, config } of poolRefs) {
      if (!config.host || !config.pass) {
        results.push({ tool: name, status: 'skip', error: 'not configured' });
        continue;
      }
      const start = Date.now();
      let pool;
      let request;
      try {
        await poolHandle.use(p => runCancellable(async ({ isCancelled }) => {
          pool = p;
          request = p.request();
          request.timeout = timeoutMs;
          if (isCancelled()) request.cancel();
          return request.query('SELECT 1');
        }, {
          signal,
          timeoutMs,
          onCancel: () => request?.cancel(),
        }), { signal, waitTimeoutMs: timeoutMs });
        results.push({ tool: name, status: 'ok', ms: Date.now() - start });
      } catch (err) {
        if (pool?.healthy === false) poolHandle.invalidate();
        results.push({ tool: name, status: 'fail', ms: Date.now() - start, error: err.message });
      }
    }
    return results;
  }

  return { names: registered, healthcheck };
}
