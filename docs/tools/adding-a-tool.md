# Adding a Tool — MySQL Example

End-to-end tutorial: give your AI agent read-only access to a MySQL database.

## What You're Building

A module called `acme` with two MySQL tools:
- `mysql_acme_prod` — production database (read-only)
- `mysql_acme_staging` — staging database (read-only)

By the end, the agent can run `SELECT` queries against both databases via MCP.

## Step 1 — Create the Module

```bash
agento module:add acme \
  --description="Acme e-commerce platform" \
  --tool mysql:mysql_acme_prod:"Production MySQL. Tables: orders, products, customers." \
  --tool mysql:mysql_acme_staging:"Staging MySQL. Same schema as production."
```

This generates `app/code/acme/` with three files:

### module.json (tool definitions + field schemas)

```json
{
  "name": "acme",
  "version": "1.0.0",
  "description": "Acme e-commerce platform",
  "tools": [
    {
      "type": "mysql",
      "name": "mysql_acme_prod",
      "description": "Production MySQL. Tables: orders, products, customers.",
      "toolset": "Acme Databases",
      "fields": {
        "host":     {"type": "string",  "label": "Host"},
        "port":     {"type": "integer", "label": "Port", "default": 3306},
        "user":     {"type": "string",  "label": "User"},
        "pass":     {"type": "obscure", "label": "Password"},
        "database": {"type": "string",  "label": "Database"}
      }
    },
    {
      "type": "mysql",
      "name": "mysql_acme_staging",
      "description": "Staging MySQL. Same schema as production.",
      "toolset": "Acme Databases",
      "fields": {
        "host":     {"type": "string",  "label": "Host"},
        "port":     {"type": "integer", "label": "Port", "default": 3306},
        "user":     {"type": "string",  "label": "User"},
        "pass":     {"type": "obscure", "label": "Password"},
        "database": {"type": "string",  "label": "Database"}
      }
    }
  ]
}
```

The `fields` block is the **schema** — it tells the framework what config each tool needs and how to handle it (`obscure` = encrypt in DB).

The `type` picks the adapter and with it the tool's capability, fixed here at review time: `mysql` is read-only, `mysql_root` grants full read/write to that one database. Both declare the same fields — see [Built-in Adapters](built-in-adapters.md#mysql-root-full-readwrite).

Every tool must declare a **`toolset`** — the group it appears under in the admin TUI **Tools** screen (one section per toolset, with a "toggle all" control). It is **required**: `agento module:validate`, `bin/test`, and `setup:upgrade` all flag a tool missing `toolset` (and `setup:upgrade` aborts before applying anything). Tools from different modules that share a `toolset` are grouped together. It has no effect on resolution or runtime — it's purely a management/grouping label (the Tools screen falls back to the module name only as a defensive default if a value is somehow absent).

### config.json (non-secret defaults)

```json
{
  "tools": {
    "mysql_acme_prod": {
      "host": "10.0.1.50",
      "port": 3306,
      "user": "acme_reader",
      "database": "acme_production"
    },
    "mysql_acme_staging": {
      "host": "10.0.1.51",
      "port": 3306,
      "user": "acme_reader",
      "database": "acme_staging"
    }
  }
}
```

This is the **lowest-priority** config layer — hosts, ports, usernames, database names. Never put passwords here (it's checked into git).

### knowledge/ directory

Empty `knowledge/README.md` — you'll add database documentation here later (table schemas, common queries, business context).

## Step 2 — Set Credentials

Passwords go into the database (encrypted automatically because the field type is `obscure`). Omit the value — agento prompts you to paste it so the secret never lands in your shell history:

```bash
agento config:set acme/tools/mysql_acme_prod/pass
# Paste…  <Ctrl+D>
agento config:set acme/tools/mysql_acme_staging/pass
# Paste…  <Ctrl+D>

# Scripting? Pipe from a file / env var instead of typing inline:
echo -n "$PROD_PW" | agento config:set acme/tools/mysql_acme_prod/pass
```

See [docs/cli/config.md#secrets](../cli/config.md#secrets--never-pass-on-the-command-line) for why.

You can also override any field via DB — useful when the same module is deployed with different hosts:

```bash
agento config:set acme/tools/mysql_acme_prod/host 10.0.2.100
```

## Step 3 — Verify Config

```bash
agento config:get acme
```

Output shows every field, its value, and where it came from:

```
acme
└ default
    tools/mysql_acme_prod/database = acme_production  [config.json]
    tools/mysql_acme_prod/host = 10.0.2.100  [db]
    tools/mysql_acme_prod/port = 3306  [config.json]
    tools/mysql_acme_prod/user = acme_reader  [config.json]
    tools/mysql_acme_prod/pass = ****
    tools/mysql_acme_staging/database = acme_staging  [config.json]
    tools/mysql_acme_staging/host = 10.0.1.51  [config.json]
    tools/mysql_acme_staging/port = 3306  [config.json]
    tools/mysql_acme_staging/user = acme_reader  [config.json]
    tools/mysql_acme_staging/pass = ****
```

Also visible in `agento admin` → Config screen.

```bash
agento tool:list
```

```
mysql_acme_prod          acme           enabled
mysql_acme_staging       acme           enabled
```

## Step 4 — Restart Toolbox

The Toolbox reads module config at startup. After adding a module, restart it:

```bash
# Dev compose
cd docker && docker compose -f docker-compose.dev.yml restart toolbox

# Production compose
cd docker && docker compose restart toolbox
```

The agent can now use `mysql_acme_prod` and `mysql_acme_staging` as MCP tools.

## How Config Resolution Works

When the Toolbox starts an MCP session, it resolves each tool field through a 3-level fallback:

```
┌──────────────────────────────────────────────────┐
│  1. ENV var (highest priority)                   │
│     CONFIG__ACME__TOOLS__MYSQL_ACME_PROD__HOST   │
├──────────────────────────────────────────────────┤
│  2. DB: core_config_data table                   │
│     path = acme/tools/mysql_acme_prod/host       │
│     Scoped: agent_view → workspace → default     │
├──────────────────────────────────────────────────┤
│  3. config.json (lowest priority)                │
│     {"tools": {"mysql_acme_prod": {"host": ...}}}│
└──────────────────────────────────────────────────┘
```

### ENV vars

Convention: `CONFIG__{MODULE}__TOOLS__{TOOL}__{FIELD}` (uppercase, hyphens → underscores).

Set in `docker/.cron.env` or `docker/.toolbox.env`:

```
CONFIG__ACME__TOOLS__MYSQL_ACME_PROD__HOST=10.0.3.200
```

### DB (core_config_data)

Written by `agento config:set`. Passwords are auto-encrypted (AES-256-CBC) when the field's type is `obscure`. DB values support scoping:

```bash
# Default (all agent_views)
agento config:set acme/tools/mysql_acme_prod/host 10.0.1.50

# Override for a specific agent_view
agento config:set acme/tools/mysql_acme_prod/host 10.0.2.100 --scope=agent_view --scope-id=1
```

Resolution order: `agent_view` → `workspace` → `default`. Most specific wins.

### config.json

Committed to git. Contains non-secret defaults shared across deployments.

### What Happens When a Field Is Missing

If a required field (like `host` or `pass`) has no value at any level, the Toolbox skips the tool with a warning in the logs. The agent won't see it as an available MCP tool.

## Per-Agent-View Tool Control

Disable a tool for a specific agent_view:

```bash
agento tool:disable mysql_acme_staging --agent-view developer
```

The tool still exists but won't be registered for that agent_view's MCP sessions.

## Adding Knowledge

Help the agent write better queries by documenting your database:

```markdown
<!-- app/code/acme/knowledge/README.md -->
# Acme Database

## Key Tables
- `orders` — id, customer_id, total, status, created_at
- `products` — id, sku, name, price, stock_qty
- `customers` — id, email, name, created_at

## Common Queries
- Order count by status: `SELECT status, COUNT(*) FROM orders GROUP BY status`
- Top products: `SELECT p.name, SUM(oi.qty) FROM order_item oi JOIN products p ON oi.product_id = p.id GROUP BY p.id ORDER BY 2 DESC LIMIT 10`
```

After editing knowledge files:

```bash
agento workspace:build --all
```

## File Summary

| File | Purpose | Where Values End Up |
|------|---------|-------------------|
| `module.json` → `tools[].fields` | Declares field schema (name, type, label) | Nowhere — it's a schema definition |
| `config.json` → `tools.{name}` | Non-secret defaults (hosts, ports) | Read at runtime as lowest-priority fallback |
| `agento config:set` | Per-installation overrides + secrets | `core_config_data` table (encrypted if `obscure`) |
| ENV vars | Deployment-level overrides | Process environment |

## Next Steps

- [Built-in Adapters](built-in-adapters.md) — MySQL, MSSQL, OpenSearch field reference
- [Config System](../config/README.md) — full 3-level fallback details
- [Creating an Adapter](creating-an-adapter.md) — add support for a new database type
- [Creating a Module](../modules/creating-a-module.md) — full module guide (events, channels, CLI commands)
