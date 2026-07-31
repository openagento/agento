import { z } from 'zod';
import mysql from 'mysql2/promise';
import { logToolboxMcp as processLog } from '../log.js';
import { runCancellable } from '../cancellable-operation.js';
import { isReadOnlySql } from './sql-read-only.js';
import { getSqlTimeoutMs } from './sql-timeout.js';

const ALLOWED_KEYWORDS = ['SELECT', 'SHOW', 'DESCRIBE', 'EXPLAIN', 'WITH'];

// Capability is bound to the declared module.json tool `type`, not to runtime config: a
// `type: "mysql"` tool can never become writable through core_config_data, ENV, or
// config.json. Granting write access requires a reviewed change to the declared type.
// Tiers differ ONLY in the guard, the advertised contract, and the pool label; an
// unrecognized tier falls back to 'read-only' so a wiring mistake fails closed.
const TIERS = {
  'read-only': {
    type: 'mysql',
    queryDescription: 'SQL query to execute (SELECT only)',
    guard: query => isReadOnlySql(query, ALLOWED_KEYWORDS, { dialect: 'mysql' }),
    blockMessage: 'Error: Only SELECT, SHOW, DESCRIBE, and EXPLAIN queries are allowed.',
    describeTool: description => description,
  },
  root: {
    type: 'mysql_root',
    // Capability must be visible in the tool NAME, not only in the manifest type: enablement is
    // keyed by name (`tools/<name>/is_enabled`) and records nothing about capability, so flipping
    // an already-enabled `mysql` tool to `mysql_root` in place would inherit its read-only grant.
    // Requiring the suffix makes every promotion a rename, and a rename needs a fresh tool:enable.
    // module_validator enforces the same rule before deploy (FULL_ACCESS_TOOL_NAME_SUFFIXES).
    nameSuffix: '_root',
    queryDescription: 'SQL query to execute — ANY SQL is allowed (full read/write: INSERT, UPDATE, DELETE, TRUNCATE, DDL)',
    guard: null,
    blockMessage: null,
    describeTool: description => `${description} FULL ACCESS (read/write): any single SQL statement executes as-is, including INSERT, UPDATE, DELETE, TRUNCATE and DDL. On timeout the statement may still have been applied — verify state before retrying, and prefer idempotent statements.`,
  },
};

// Reserved both ways: a full-access tier must use its suffix, and no other tier may. Only the
// pair makes promotion a rename — otherwise a read-only tool could take the name first, be
// enabled, and later be escalated by an edit to its `type` alone. module_validator enforces the
// same rule before deploy (FULL_ACCESS_TOOL_NAME_SUFFIXES / RESERVED_TOOL_NAME_SUFFIXES); this is
// the runtime backstop.
const RESERVED_NAME_SUFFIXES = Object.values(TIERS).map(t => t.nameSuffix).filter(Boolean);

function nameViolation(toolName, tier) {
  const required = TIERS[tier].nameSuffix;
  if (required) {
    return toolName.endsWith(required)
      ? null
      : `a "${TIERS[tier].type}" tool grants full read/write, so its name must end in "${required}"`;
  }
  const squatted = RESERVED_NAME_SUFFIXES.find(suffix => toolName.endsWith(suffix));
  return squatted
    ? `"${squatted}" is reserved for full-access tool types, so a "${TIERS[tier].type}" tool must not use it`
    : null;
}

function createMysqlTool(server, toolName, description, config, options) {
  const tier = TIERS[options.tier] || TIERS['read-only'];
  // The session logger carries the agent_view label/id, so every decision this tool logs is
  // attributable to a scope — not only to the LLM-supplied `user`. Falls back to the
  // process-wide MCP logger for interactive runs and tool-list sessions with no agent_view.
  const log = options.log || processLog;
  const port = parseInt(config.port || '3306');
  const configuredPoolMax = Number.parseInt(config.client_connection_pool_max_per_tool, 10);
  const poolMax = Number.isInteger(configuredPoolMax) && configuredPoolMax > 0
    ? configuredPoolMax
    : options.clientConnectionPoolMaxPerTool;
  const mysqlConfig = {
    host: config.host,
    port,
    user: config.user,
    password: config.pass,
    database: config.database,
    waitForConnections: true,
    connectionLimit: poolMax,
  };
  const poolHandle = options.sqlPoolRegistry.createPoolHandle({
    adapter: tier.type,
    toolName,
    config: mysqlConfig,
    server: { host: String(config.host).trim().toLowerCase(), port },
    serverConcurrencyBudget: options.serverConcurrencyBudget,
    queueWaitTimeoutMs: options.sqlTimeoutMs || 300_000,
    create: () => mysql.createPool(mysqlConfig),
    close: pool => pool.end(),
  });

  server.tool(
    toolName,
    tier.describeTool(description),
    {
      user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
      query: z.string().describe(tier.queryDescription),
    },
    async ({ user, query }) => {
      if (!config.host || !config.pass) {
        log(toolName, 'ERROR', `user=${user} type=${tier.type} - not configured (missing host or pass)`);
        return {
          content: [{ type: 'text', text: `Error: ${toolName} not configured. Set host and pass via bin/agento config:set or ENV vars.` }],
          isError: true,
        };
      }

      if (tier.guard && !tier.guard(query)) {
        log(toolName, 'BLOCKED', `user=${user} type=${tier.type} non-readonly query: ${query.substring(0, 80)}`);
        return {
          content: [{ type: 'text', text: tier.blockMessage }],
          isError: true,
        };
      }

      log(toolName, 'QUERY', `user=${user} type=${tier.type} | ${query}`);
      const start = Date.now();

      try {
        const [rows] = await poolHandle.use(pool => pool.query({ sql: query, timeout: options.sqlTimeoutMs }));
        const elapsed = Date.now() - start;
        const rowCount = Array.isArray(rows) ? rows.length : '?';

        log(toolName, 'OK', `user=${user} type=${tier.type} time=${elapsed}ms rows=${rowCount}`);

        const text = JSON.stringify(rows, null, 2);
        return { content: [{ type: 'text', text }] };
      } catch (err) {
        log(toolName, 'ERROR', `user=${user} type=${tier.type} ${err.message}`);
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

function registerTierTools(server, tools, options, tier) {
  if (tools.length > 0 && !options.sqlPoolRegistry) {
    throw new Error('MySQL adapter requires sqlPoolRegistry');
  }
  const resolvedOptions = {
    clientConnectionPoolMaxPerTool: options.clientConnectionPoolMaxPerTool || 10,
    serverConcurrencyBudget: options.serverConcurrencyBudget || 10,
    sqlPoolRegistry: options.sqlPoolRegistry,
    sqlTimeoutMs: getSqlTimeoutMs(options.sqlTimeoutSeconds),
    log: options.log,
    tier,
  };
  const registered = [];
  const poolRefs = [];

  for (const tool of tools) {
    const violation = nameViolation(tool.name, tier);
    if (violation) {
      (options.log || processLog)(tool.name, 'ERROR', `refused: ${violation} — not registered`);
      continue;
    }
    const poolHandle = createMysqlTool(server, tool.name, tool.description, tool.config, resolvedOptions);
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
      let connection = null;
      let cancelled = false;
      try {
        await poolHandle.use(pool => runCancellable(async ({ isCancelled }) => {
          connection = await pool.getConnection();
          if (isCancelled()) {
            connection.destroy();
            throw new Error('Healthcheck cancelled');
          }
          await connection.query('SELECT 1');
        }, {
          signal,
          timeoutMs,
          onCancel: () => {
            cancelled = true;
            connection?.destroy();
          },
        }), { signal, waitTimeoutMs: timeoutMs });
        results.push({ tool: name, status: 'ok', ms: Date.now() - start });
      } catch (err) {
        results.push({ tool: name, status: 'fail', ms: Date.now() - start, error: err.message });
      } finally {
        if (connection && !cancelled) connection.release();
      }
    }
    return results;
  }

  return { names: registered, healthcheck };
}

/**
 * Register read-only MySQL tools (module.json `type: "mysql"`) from pre-resolved tool configs.
 * Every query passes the isReadOnlySql guard — SELECT/SHOW/DESCRIBE/EXPLAIN/WITH only.
 * @param {object} server - MCP server
 * @param {Array<{name, description, config}>} tools - Resolved tool configs from config-loader
 * @returns {{ names: string[], healthcheck: () => Promise<Array> }} Registered tool names and healthcheck function
 */
export function registerMysqlTools(server, tools, options = {}) {
  return registerTierTools(server, tools, options, 'read-only');
}

/**
 * Register FULL-ACCESS MySQL tools (module.json `type: "mysql_root"`). No read-only guard:
 * any single statement runs as-is. The database user's GRANTs are the actual boundary, so back
 * these tools with a least-privilege login scoped to their own database. Multi-statement
 * stacking stays blocked by the mysql2 default `multipleStatements: false`.
 * @param {object} server - MCP server
 * @param {Array<{name, description, config}>} tools - Resolved tool configs from config-loader
 * @returns {{ names: string[], healthcheck: () => Promise<Array> }} Registered tool names and healthcheck function
 */
export function registerMysqlRootTools(server, tools, options = {}) {
  return registerTierTools(server, tools, options, 'root');
}
