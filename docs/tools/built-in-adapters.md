# Built-in Tool Adapters

Four adapter types ship with Agento (`mysql`, `mysql_root`, `mssql`, `opensearch`). Each reads config from the module's field schema.

**The declared `type` in module.json IS the capability.** Adapter dispatch is by `type`, so a tool's SQL privileges are fixed at declaration time in git-tracked `module.json` — granted at commit/review time. Nothing at runtime (a `core_config_data` row, an ENV var, a `config.json` edit) can turn a `type: "mysql"` tool into a writable one; that requires a reviewed change to the declared type.

## MySQL (read-only)

Read-only SQL queries against MySQL/MariaDB databases.

**Type:** `mysql`
**Required fields:** `host`, `pass`
**Optional fields:** `port` (default: 3306), `user`, `database`

**Enforced:** Only `SELECT`, `SHOW`, `DESCRIBE`, `EXPLAIN`, `WITH` queries allowed. Rejected queries never reach the driver and are logged as `BLOCKED`.

**Timeout:** Controlled by `core/sql_timeout_seconds` (default: 300s) through the standard config fallback.

Source: [src/agento/toolbox/adapters/mysql.js](../../src/agento/toolbox/adapters/mysql.js)

## MySQL root (full read/write)

Full-access SQL against a MySQL/MariaDB database — for a database the agent is deliberately meant to own, such as its dedicated sandbox.

**Type:** `mysql_root`
**Fields:** identical to `mysql` (`host`, `port`, `user`, `pass`, `database`, `client_connection_pool_max_per_tool`). No new config field — the type is the capability.

**Naming rule:** a `mysql_root` tool's name MUST end in `_root` (e.g. `mysql_sandbox_root`). This is enforced by `module:add`, `module:validate`, and the adapter itself — see [SECURITY](#security) for why.

**Enforced:** nothing at the app layer. There is no `isReadOnlySql` guard: any single statement executes as-is (`INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `CREATE`, `ALTER`, `DROP`, …). The tool's MCP description and its `query` parameter both state this, so the agent is not misled about what it may run.

Multi-statement stacking (`INSERT …; DROP …`) is still refused: the mysql2 driver runs with its default `multipleStatements: false`, so the batch is sent as one statement and the server rejects it. The adapter never splits statements itself.

Pooling, timeouts, and healthchecks are shared with `mysql`, but pools are keyed under a distinct `mysql_root` adapter label. That keeps logs and metrics separable, and means a `mysql_root` tool gets its **own** `core/server_concurrency_budget` bucket for a host it shares with `mysql` tools.

```json
{
  "tools": [
    {
      "type": "mysql_root",
      "name": "mysql_sandbox_root",
      "description": "Agent sandbox MySQL — scratch schema, safe to modify.",
      "toolset": "sandbox",
      "fields": {
        "host": {"type": "string", "label": "Host"},
        "port": {"type": "integer", "label": "Port", "default": 3306},
        "user": {"type": "string", "label": "User"},
        "pass": {"type": "obscure", "label": "Password"},
        "database": {"type": "string", "label": "Database"}
      }
    }
  ]
}
```

`agento module:add sandbox --tool mysql_root:mysql_sandbox_root:"Agent sandbox MySQL"` scaffolds the same field set.

### SECURITY

`mysql_root` has **no app-layer guard**, so the database user's `GRANT`s are the ACTUAL boundary. Treat the DB login as the security control:

- Back every `mysql_root` tool with a least-privilege login scoped to that one database — e.g. `GRANT ALL PRIVILEGES ON sandbox.* TO 'agent_sandbox'@'%'`. No `FILE`, `SUPER`, `PROCESS`, `RELOAD`, or `GRANT OPTION` unless you specifically intend them (`FILE` alone reads and writes server-side files via `LOAD DATA INFILE` / `INTO OUTFILE`).
- Never point a `mysql_root` tool at a database that also serves production reads, and never reuse a login that has rights on other schemas.
- Enable `mysql_root` tools ONLY on agent_views that do not ingest untrusted content. The harness runs agents with skip-permissions, so a prompt-injected instruction in an inbound email, Jira comment, or web page becomes a `DELETE` the agent will happily run.
- The opt-in gate still applies on top: like every tool, a `mysql_root` tool is disabled by default and only available where `tools/<name>/is_enabled` resolves to `1` (agent_view > workspace > default). Declaring one grants no access until it is explicitly enabled — prefer `agento tool:enable mysql_sandbox_root --agent-view <code>` over a default-scope enable.
- **`_root` is a RESERVED suffix, enforced in both directions.** Enablement is keyed by tool NAME (`tools/<name>/is_enabled`) and records nothing about capability, so an in-place `type` edit would otherwise carry an existing grant straight over. Two rules together close that: a `mysql_root` tool's name MUST end in `_root`, and **no other tool type may use that suffix** — so a read-only tool cannot squat the name first and be escalated later by editing only its `type`. Each rule alone is useless; the pair is what makes promotion a rename, and a rename means a name with no `is_enabled` row. Enforced by `agento module:add` (rejects the tool spec), `module:validate` — and therefore `setup:upgrade` — (rejects the manifest), and the mysql adapter at runtime (logs an `ERROR`, leaves the tool unregistered). Reviewers should still treat any manifest diff that changes a tool's `type` as a privilege grant.
- **Known residual gap: a stale `is_enabled` row can outlive its tool.** The reserved suffix binds a NAME to a capability, not a GRANT to a capability. If a `mysql_root` tool is deleted from a manifest, its `tools/<name>/is_enabled` row survives in `core_config_data`, and a later tool reusing that exact name inherits it. The name can only ever have belonged to a full-access tool, so the row was a deliberate full-access grant — but it was granted for a different tool. Run `agento config:remove tools/<name>/is_enabled --scope=... --scope-id=...` when you retire a `mysql_root` tool. Closing this properly needs a capability-scoped enablement gate (a grant recorded against the capability, not just the name).
- **A timed-out write may still commit.** `core/sql_timeout_seconds` is enforced as a mysql2 client-side inactivity timeout: it rejects the tool call but does not send `KILL QUERY`, so the server keeps executing the statement. Treat a timeout on a `mysql_root` tool as an UNKNOWN outcome — verify state before retrying, and prefer idempotent statements (`INSERT … ON DUPLICATE KEY UPDATE`, `DELETE … WHERE id = …`) so a retry cannot double-apply. The tool's own description tells the agent the same thing. This is a property of the shared SQL execution path — read-only `mysql` and `mssql` time out the same way — but only writes can leave a lasting effect.
- Existing `type: "mysql"` tools are unaffected: their code path is untouched and remains provably read-only.
- Every decision a MySQL tool logs (`QUERY`, `OK`, `ERROR`, and `BLOCKED` for the read-only tier) goes to the session's agent_view-scoped logger, so `toolbox_mcp.log` attributes destructive statements to an agent_view — not only to the `user` argument the LLM supplies.

## MSSQL

Read-only SQL queries against Microsoft SQL Server.

**Required fields:** `host`, `pass`
**Optional fields:** `port` (default: 1433), `user`, `database`

**Enforced:** Only `SELECT`, `WITH` queries allowed.

Source: [src/agento/toolbox/adapters/mssql.js](../../src/agento/toolbox/adapters/mssql.js)

## OpenSearch

Query OpenSearch/Elasticsearch indices.

**Required fields:** `host` (URL including protocol), `pass`
**Optional fields:** `user`

Supports: index info (GET) and `_search` queries (POST with JSON body).

Source: [src/agento/toolbox/adapters/opensearch.js](../../src/agento/toolbox/adapters/opensearch.js)

## module.json Example

```json
{
  "tools": [
    {
      "type": "mysql",
      "name": "mysql_myapp_prod",
      "description": "My App Production MySQL. Tables: users, orders.",
      "toolset": "myapp",
      "fields": {
        "host": {"type": "string", "label": "Host"},
        "port": {"type": "integer", "label": "Port", "default": 3306},
        "user": {"type": "string", "label": "User"},
        "pass": {"type": "obscure", "label": "Password"},
        "database": {"type": "string", "label": "Database"},
        "client_connection_pool_max_per_tool": {"type": "integer", "label": "Maximum client connections"}
      }
    }
  ]
}
```

## SQL Timeout

Set globally through the standard config path (or its ENV equivalent):

```bash
agento config:set core/sql_timeout_seconds 300
# ENV: CONFIG__CORE__SQL_TIMEOUT_SECONDS=300
```

Source: [src/agento/toolbox/adapters/sql-timeout.js](../../src/agento/toolbox/adapters/sql-timeout.js)

## SQL Connection Pools

MySQL (both `mysql` and `mysql_root`) and MSSQL pools are scoped to the adapter type, tool name, and fully resolved connection configuration. Identical configurations reuse one lazy pool across MCP sessions; different tools never share one, even when they target the same server.

Each active tool configuration is limited by `core/client_connection_pool_max_per_tool` (default 10). Override one tool with `<module>/tools/<tool>/client_connection_pool_max_per_tool`. A pool that has no active operation for 30 seconds is closed, and all SQL pools are closed when the toolbox receives `SIGTERM`.

All pools targeting the same adapter, host, and port share `core/server_concurrency_budget` (default 10). This is a process-wide limit on active database operations, regardless of tool, database, credentials, or agent_view. It does not multiply by the number of pools. The setting is default-scope only so scoped sessions cannot create conflicting server-wide budgets. At most 100 operations may wait per server endpoint; queued operations are cancelled when their SQL deadline expires (or an AbortSignal is supplied and aborted).

SQL healthchecks use the same server budget but are actively cancelled at the health endpoint deadline: MySQL destroys its borrowed connection and MSSQL cancels its request. A timed-out `/health?test=true` therefore cannot leave an invisible query occupying the shared budget.

```bash
# Defaults applied to every SQL tool and every DB server endpoint
agento config:set core/client_connection_pool_max_per_tool 10
agento config:set core/server_concurrency_budget 10

# Optional override for one tool's client pool
agento config:set acme/tools/mysql_acme_prod/client_connection_pool_max_per_tool 20
```

## Large Result Offload

The framework automatically wraps ALL tool handlers with result offload middleware. When a tool result exceeds a configurable size threshold, the full result is saved to disk and a summary is returned to the agent instead. This prevents oversized responses from consuming agent context window.

The middleware is applied transparently via the `server.tool` wrapper in `config-loader.js` -- individual adapters do not need to implement offload logic.

### Result strategies

Each tool can declare a `resultStrategy` via the optional 5th argument to `server.tool()`:

| Strategy | Behavior |
|----------|----------|
| `'text'` (default) | Offloads to `.txt` when total text content exceeds threshold |
| `'rows'` | Tries to parse each text content item as a JSON array. If found, offloads to `.csv` with column headers and sample rows. Falls back to `.txt` if no valid JSON array. |
| `false` | Explicitly opt out of offload wrapping |

```js
// Your tool gets automatic text offload (default)
server.tool('my_tool', 'description', schema, handler);

// Opt into CSV offload for tabular results
server.tool('my_db_tool', 'description', schema, handler, { resultStrategy: 'rows' });

// Explicitly opt out (e.g., binary/streaming tools)
server.tool('my_special_tool', 'description', schema, handler, { resultStrategy: false });
```

Built-in database adapters (MySQL, MySQL root, MSSQL, OpenSearch) use `resultStrategy: 'rows'`.

### Config paths

Config paths (core module, 3-level fallback):

| Path | Default | Description |
|------|---------|-------------|
| `core/toolbox/result_offload/threshold` | 20000 | Size threshold in bytes (estimated via JSON.stringify) |
| `core/toolbox/result_offload/sample_rows` | 5 | Number of sample rows included in the summary |
| `core/toolbox/result_offload/text_preview_chars` | 200 | Number of preview characters for text offload |

Files are written to `${artifactsDir}/mcp-results/{toolName}/result_{timestamp}.{csv,txt}` (where `artifactsDir` is `/workspace/artifacts/{workspace}/{agent_view}/{job_id}`). Cleanup of old offload files is the responsibility of the artifacts dir lifecycle manager, not this middleware.

Override per agent_view:
```bash
agento config:set core/toolbox/result_offload/threshold 50000 --scope=agent_view --scope-id=1
```

Source: [src/agento/toolbox/adapters/large-result.js](../../src/agento/toolbox/adapters/large-result.js)
