# core_config_data

Database table for config overrides — identical to Magento's `core_config_data`.

## Table Schema

```sql
CREATE TABLE core_config_data (
    config_id  INT AUTO_INCREMENT PRIMARY KEY,
    scope      VARCHAR(8) NOT NULL DEFAULT 'default',
    scope_id   INT NOT NULL DEFAULT 0,
    path       VARCHAR(255) NOT NULL,
    value      TEXT NULL,
    encrypted  TINYINT(1) NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_scope_path (scope, scope_id, path)
);
```

## Path Format

`{module}/tools/{tool_name}/{field_name}`

```
my_app/tools/mysql_prod/host             → "10.0.0.1"
my_app/tools/mysql_prod/pass             → "aes256:iv:ciphertext" (encrypted=1)
nav_erp/tools/mssql_nav/database        → "NAV_Production"
```

## Scope

`scope` and `scope_id` follow Magento conventions (like `bin/magento config:set --scope=websites --scope-id=1`):

| Scope | scope_id | Description |
|-------|----------|-------------|
| `default` | `0` | Global (default) |
| `workspace` | workspace ID | Per-workspace override |
| `agent_view` | agent_view ID | Per-agent_view override (most specific) |

Resolution order: agent_view → workspace → default (most specific wins).

## CLI

```bash
# Set a secret (omit value → agento prompts / reads stdin, nothing in bash history)
bin/agento config:set my_app/tools/mysql_prod/pass
# Paste…  <Ctrl+D>

# Set a plain value on the command line is fine
bin/agento config:set core/timezone UTC

# Set a scoped value (per agent_view)
bin/agento config:set core/allowed_domains "example.com" --scope=agent_view --scope-id=1

# --agent-view <code> is a shortcut that resolves the scope_id for you
bin/agento config:set core/allowed_domains "example.com" --agent-view dev_01

# Set a workspace-scoped value
bin/agento config:set core/email_whitelist "*@corp.com" --scope=workspace --scope-id=2

# Get exact path (shows per-scope values, deduplicates if identical)
bin/agento config:get core/allowed_domains
#   core/allowed_domains = example.com              [default]
#   core/allowed_domains = google.com,github.com [agent_view: Agento01]

# Get module tree (shows all config grouped by scope tier)
bin/agento config:get jira
#   jira
#   ├ default
#   │   tools/mysql_magento_prod/host = 10.0.0.1
#   │   tools/mysql_magento_prod/port = 3306     [config.json]
#   ├ agent_view: Agento01 (id=1)
#   │   core/allowed_domains = google.com,github.com
#   └

# Remove a config override (falls back to config.json default)
bin/agento config:remove my_app/tools/mysql_prod/host
bin/agento config:remove core/allowed_domains --scope=agent_view --scope-id=1

# List all values (all scopes)
bin/agento config:list

# List by module prefix
bin/agento config:list jira
```

## Framework Config Paths

The `core` module provides these framework-level config paths:

| Path | Type | Default | Description |
|------|------|---------|-------------|
| `core/timezone` | string | `"UTC"` | IANA timezone (e.g., `Europe/Warsaw`, `America/New_York`). Scoped per agent_view/workspace. |
| `core/sql_timeout_seconds` | integer | `300` | SQL query timeout |
| `core/client_connection_pool_max_per_tool` | integer | `10` | Maximum client connections in one MySQL/MSSQL tool pool |
| `core/server_concurrency_budget` | integer | `10` | Process-wide maximum active SQL operations per adapter/host/port; default scope only |
| `core/email_whitelist` | string | — | Allowed email recipients (comma-separated) |
| `core/allowed_domains` | string | — | Allowed browser domains (comma-separated) |
| `core/toolbox/url` | string | `"http://toolbox:3001"` | Toolbox base URL. Each harness's `WorkspaceAdapter` appends its transport path (`/mcp` for both Claude and Codex). Jira also reads this for REST calls. |

All DB timestamps are stored in UTC. The `core/timezone` setting is for code that needs to reason in local time (e.g., idempotency key bucketing, future display). ENV override: `CONFIG__CORE__TIMEZONE`.

## Only Overrides

This table stores **only explicit overrides** — values set via `config:set`. Default values from `config.json` are NOT stored here (unlike Magento where defaults are sometimes imported).

The config loader merges at runtime: ENV → DB overrides → config.json defaults.

Source: [src/agento/framework/core_config.py](../../src/agento/framework/core_config.py)
