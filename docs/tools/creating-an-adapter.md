# Creating a Tool Adapter

Add a new database or service type (e.g., PostgreSQL, Redis, MongoDB).

## 1. Create the Adapter File

Create `src/agento/toolbox/adapters/postgres.js`:

```javascript
import { z } from 'zod';
import pg from 'pg';
import { logToolboxMcp as log } from '../log.js';
import { isReadOnlySql } from './sql-read-only.js';
import { getSqlTimeoutMs } from './sql-timeout.js';

function createPostgresTool(server, toolName, description, config, options) {
  const port = parseInt(config.port || '5432', 10);
  const poolConfig = {
    host: config.host,
    port,
    user: config.user,
    password: config.pass,
    database: config.database,
    max: options.clientConnectionPoolMaxPerTool,
  };
  const poolHandle = options.sqlPoolRegistry.createPoolHandle({
    adapter: 'postgres',
    toolName,
    config: poolConfig,
    server: { host: String(config.host).trim().toLowerCase(), port },
    serverConcurrencyBudget: options.serverConcurrencyBudget,
    queueWaitTimeoutMs: getSqlTimeoutMs(options.sqlTimeoutSeconds),
    create: () => new pg.Pool(poolConfig),
    close: pool => pool.end(),
  });

  server.tool(
    toolName,
    description,
    {
      user: z.string().email().describe('Agent email from SOUL.md'),
      query: z.string().describe('SQL query (SELECT only)'),
    },
    async ({ user, query }) => {
      if (!isReadOnlySql(query, ['SELECT', 'WITH'], { dialect: 'postgresql' })) {
        log(toolName, 'BLOCKED', `user=${user} non-readonly query`);
        return {
          content: [{ type: 'text', text: 'Error: Only read-only SELECT queries are allowed.' }],
          isError: true,
        };
      }
      const result = await poolHandle.use(pool => pool.query(query));
      return { content: [{ type: 'text', text: JSON.stringify(result.rows) }] };
    }
  );

  return poolHandle;
}

export function registerPostgresTools(server, tools, options) {
  const registered = [];
  const poolRefs = [];

  for (const tool of tools) {
    const poolHandle = createPostgresTool(
      server,
      tool.name,
      tool.description,
      tool.config,
      options
    );
    registered.push(tool.name);
    poolRefs.push({ name: tool.name, config: tool.config, poolHandle });
  }

  async function healthcheck() {
    const results = [];
    for (const { name, config, poolHandle } of poolRefs) {
      if (!config.host || !config.pass) {
        results.push({ tool: name, status: 'skip', error: 'not configured' });
        continue;
      }
      const start = Date.now();
      try {
        // Adapter-specific connectivity check
        await poolHandle.use(pool => pool.query('SELECT 1'));
        results.push({ tool: name, status: 'ok', ms: Date.now() - start });
      } catch (err) {
        results.push({ tool: name, status: 'fail', ms: Date.now() - start, error: err.message });
      }
    }
    return results;
  }

  return { names: registered, healthcheck };
}
```

## 2. Register in ADAPTERS Map

Edit `src/agento/toolbox/adapters/index.js`:

```javascript
import { registerPostgresTools } from './postgres.js';

const ADAPTERS = {
  mysql: registerMysqlTools,
  mysql_root: registerMysqlRootTools,
  mssql: registerMssqlTools,
  opensearch: registerOpensearchTools,
  postgres: registerPostgresTools,  // NEW
};
```

That's it. The config-loader already handles any type — it just passes resolved config to the adapter.

## 3. Use in a Module

```json
{
  "tools": [{
    "type": "postgres",
    "name": "pg_analytics",
    "description": "Analytics PostgreSQL database",
    "fields": {
      "host": {"type": "string", "label": "Host"},
      "port": {"type": "integer", "label": "Port", "default": 5432},
      "user": {"type": "string", "label": "User"},
      "pass": {"type": "obscure", "label": "Password"},
      "database": {"type": "string", "label": "Database"}
    }
  }]
}
```

## 4. Install npm Package

```bash
cd src/agento/toolbox && npm install pg
```

## Adapter Contract

Each adapter exports a register function with this signature:

```typescript
function registerXxxTools(
  server: McpServer,
  tools: Array<{ name: string, description: string, config: Record<string, any> }>,
  options: {
    sqlTimeoutSeconds: number,
    clientConnectionPoolMaxPerTool: number,
    serverConcurrencyBudget: number,
    sqlPoolRegistry: SqlPoolRegistry
  }
): { names: string[], healthcheck: () => Promise<Array<{ tool: string, status: 'ok' | 'fail' | 'skip', ms?: number, error?: string }>> }
```

The `config` object contains resolved values from the 3-level fallback. Fields match the `fields` schema in module.json.

SQL adapters must use the process-owned `sqlPoolRegistry` supplied in `options`. Do not keep a module-level pool or registry: adapter registrations happen per MCP session, while safe pool reuse and the per-server concurrency budget are process-wide concerns managed by the injected registry. They must also enforce read-only SQL before calling the driver, using the matching dialect; a read-only database principal remains mandatory defense in depth.

## Write Access Is a Separate Type, Never a Config Flag

The only sanctioned way to grant write access is a **separate adapter type with its own registrar** — `mysql_root` next to `mysql`, never a `read_only: false` config field. Capability then lives in git-tracked `module.json`, granted at review time, and no `core_config_data` row or ENV var can promote a read-only tool.

`mysql.js` shows the shape to copy for an `mssql_root` / `postgres_root` follow-up: one `TIERS` table holding the per-tier guard, query-parameter description, tool-description decoration, required tool-name suffix, and pool `adapter` label; one shared `createXxxTool` / `registerTierTools` pair so pool wiring, timeouts, logging, and healthchecks are literally the same code; and two thin exports that differ only in the tier they pass. The read-only path keeps its exact guard and block message, so adding a tier cannot change existing tools' decisions.

A full-access tier must also:

- Say so in both the tool description and the `query` parameter description, including that a timed-out statement may still have been applied.
- Require a capability marker in the tool NAME, registered in `module_validator.FULL_ACCESS_TOOL_NAME_SUFFIXES` so `module:add` and `module:validate` enforce it too. Tool enablement is keyed by name and records nothing about capability, so without a marker an in-place `type` edit would inherit an existing read-only grant. The suffix must be enforced in BOTH directions — required on the full-access type, and reserved from every other type — or a tool can simply squat the name before being escalated. Reuse the existing `_root` suffix rather than inventing a per-adapter one.
- Document that the database user's `GRANT`s are the real boundary.

Every adapter should log through `options.log` when present (the session's agent_view-scoped logger), falling back to the process-wide MCP logger — otherwise destructive invocations are attributed only to the LLM-supplied `user` argument.

The shared validator deliberately rejects backslashes inside PostgreSQL string literals. PostgreSQL `E'...'` strings always interpret backslash escapes, while plain strings depend on server settings; rejecting the ambiguous form prevents the validator and server from disagreeing about where a statement ends.

The `healthcheck` function is called by `/health?test=true` to verify connectivity. It should return one result per tool: `ok` (connected), `fail` (error), or `skip` (not configured).

## Module JS Tool Healthchecks

Convention-based JS tools (`toolbox/*.js`) can also export a `healthcheck(context)` function alongside `register()`. It receives the same context and returns an array of results:

```javascript
export async function healthcheck({ moduleConfigs, db }) {
  const start = Date.now();
  try {
    await db.getCronPool().query('SELECT 1');
    return [{ tool: 'my_tool', status: 'ok', ms: Date.now() - start }];
  } catch (err) {
    return [{ tool: 'my_tool', status: 'fail', ms: Date.now() - start, error: err.message }];
  }
}
```

For tools that cover a group (e.g. all `jira_*` tools), use a group name like `jira` — the test script maps group names to individual tools by prefix.

## Reference

Use [src/agento/toolbox/adapters/mysql.js](../../src/agento/toolbox/adapters/mysql.js) as a template — it covers read-only enforcement, error handling, logging, connection pooling, and healthcheck.
