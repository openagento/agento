import { registerMysqlTools, registerMysqlRootTools } from './mysql.js';
import { registerMssqlTools } from './mssql.js';
import { registerOpensearchTools } from './opensearch.js';

// Adapter type == capability. "mysql" is read-only; "mysql_root" is full read/write. The type is
// declared in git-tracked module.json, so write access is granted at review time and no runtime
// config can promote a "mysql" tool. See docs/tools/built-in-adapters.md.
const ADAPTERS = {
  mysql: registerMysqlTools,
  mysql_root: registerMysqlRootTools,
  mssql: registerMssqlTools,
  opensearch: registerOpensearchTools,
};

// Tool types that are NOT config-driven DB adapters but declarative, self-registering JS tools — a
// module's toolbox/*.js calls server.tool() itself (e.g. type "mcp" for the outlook tools). These have
// no adapter here by design, so they must not trigger the "No adapter for tool type" warning.
const DECLARATIVE_TYPES = new Set(['mcp']);

function positiveInteger(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

/**
 * Register config-driven adapter tools (mysql, mssql, opensearch) from module.json declarations.
 * @param {object} moduleConfigs - Resolved module-level configs (for sql_timeout_seconds etc.)
 * @returns {{ names: string[], healthchecks: Array<() => Promise<Array>> }}
 */
export function registerAdapterTools(
  server,
  allTools,
  moduleToolTypes,
  moduleConfigs = {},
  { sqlPoolRegistry, log } = {}
) {
  const dynamicNames = [];
  const healthchecks = [];
  const sqlTimeoutSeconds = parseInt(moduleConfigs?.core?.sql_timeout_seconds || '300', 10);
  const clientConnectionPoolMaxPerTool = positiveInteger(
    moduleConfigs?.core?.client_connection_pool_max_per_tool,
    10
  );
  const serverConcurrencyBudget = positiveInteger(
    moduleConfigs?.core?.server_concurrency_budget,
    10
  );

  for (const [type, registerFn] of Object.entries(ADAPTERS)) {
    const tools = allTools.filter(t => t.type === type);
    const { names, healthcheck } = registerFn(server, tools, {
      sqlTimeoutSeconds,
      clientConnectionPoolMaxPerTool,
      serverConcurrencyBudget,
      sqlPoolRegistry,
      log,
    });
    dynamicNames.push(...names);
    healthchecks.push(healthcheck);
  }

  // Warn about unknown tool types (declarative self-registering types are expected, not unknown)
  for (const type of moduleToolTypes) {
    if (!ADAPTERS[type] && !DECLARATIVE_TYPES.has(type)) {
      console.error(
        `[tools] No adapter for tool type "${type}". Registered adapters: ${Object.keys(ADAPTERS).join(', ')}`
      );
    }
  }

  return { names: dynamicNames, healthchecks };
}
