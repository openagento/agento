# ENV Variables

ENV vars have the highest config priority — they override DB and config.json values.

> **Exception: a field may refuse the ENV source.** `"allowEnv": false` in `system.json` makes both
> resolvers ignore that field's `CONFIG__*` var, and `module:validate` **fails a deploy** that sets one.
> A field with `"access": "toolbox_only"` goes further: Python never resolves it at all, from any
> source. Use both for credentials that must stay inside the toolbox — a container environment is
> plaintext, readable by anything that can run `docker inspect`. See
> [Access Restrictions](README.md#access-restrictions-access-allowenv).

## Convention

```
CONFIG__{MODULE}__{PATH}
```

- Prefix: `CONFIG__` (double underscore)
- Module: module name, uppercase, hyphens → underscores
- Path: config path from config.json, `/` → `__`, uppercase

## Examples

### Module-level fields

| Config Path | ENV Variable |
|-------------|-------------|
| `jira/user` | `CONFIG__JIRA__USER` |
| `jira/jira_host` | `CONFIG__JIRA__JIRA_HOST` |
| `jira/attachment_download_limit` | `CONFIG__JIRA__ATTACHMENT_DOWNLOAD_LIMIT` |
| `core/sql_timeout_seconds` | `CONFIG__CORE__SQL_TIMEOUT_SECONDS` |
| `core/client_connection_pool_max_per_tool` | `CONFIG__CORE__CLIENT_CONNECTION_POOL_MAX_PER_TOOL` |
| `core/server_concurrency_budget` | `CONFIG__CORE__SERVER_CONCURRENCY_BUDGET` |
| `core/allowed_domains` | `CONFIG__CORE__ALLOWED_DOMAINS` |

### Tool-level fields

| Config Path | ENV Variable |
|-------------|-------------|
| `my-ecommerce/tools/mysql_ecom_prod/host` | `CONFIG__MY_ECOMMERCE__TOOLS__MYSQL_ECOM_PROD__HOST` |
| `my-ecommerce/tools/mysql_ecom_prod/pass` | `CONFIG__MY_ECOMMERCE__TOOLS__MYSQL_ECOM_PROD__PASS` |
| `nav-erp/tools/mssql_nav/host` | `CONFIG__NAV_ERP__TOOLS__MSSQL_NAV__HOST` |
| `nav-erp/tools/mssql_nav/client_connection_pool_max_per_tool` | `CONFIG__NAV_ERP__TOOLS__MSSQL_NAV__CLIENT_CONNECTION_POOL_MAX_PER_TOOL` |
| `my-app/tools/opensearch_prod/host` | `CONFIG__MY_APP__TOOLS__OPENSEARCH_PROD__HOST` |

## Where to Set

**secrets.env** (loaded by toolbox via docker-compose env_file):
```env
CONFIG__MY_ECOMMERCE__TOOLS__MYSQL_ECOM_PROD__HOST=10.0.0.1
CONFIG__MY_ECOMMERCE__TOOLS__MYSQL_ECOM_PROD__PASS=secret
```

**docker-compose.yml** (per-container):
```yaml
toolbox:
  environment:
    - CONFIG__MY_ECOMMERCE__TOOLS__MYSQL_ECOM_PROD__HOST=10.0.0.1
```

## JSON Values — Use DB, Not ENV

Docker Compose `env_file` strips double quotes from values, which breaks JSON syntax. For fields declared as `type: "json"` in `system.json` (e.g. `jira_projects`, `frequency_map`), use DB overrides instead:

```bash
bin/agento config:set jira/jira_projects '["AI", "K3"]'
bin/agento config:set jira/frequency_map '{"Daily":"0 8 * * *"}'
```

Only scalar values (strings, integers, booleans) work reliably in env files.

## When to Use ENV vs DB

| Scenario | Use |
|----------|-----|
| Docker/K8s secrets management | ENV |
| Different values per environment (staging/prod) | ENV |
| Set once during installation | DB (`config:set`) |
| Shared across team deployments | config.json |

## ENV Files

| File | Container | Purpose |
|------|-----------|---------|
| `docker/.cron.env` | cron | Module config (`CONFIG__JIRA__*`), framework flags (`DISABLE_LLM`) |
| `docker/.toolbox.env` | toolbox | Module config (`CONFIG__CORE__*`, `CONFIG__JIRA__*`) |
| `secrets.env` | toolbox only | Sensitive config (`CONFIG__JIRA__JIRA_TOKEN`, `CONFIG__CORE__SMTP_PASS`) |

Source: [src/agento/toolbox/config-loader.js](../../src/agento/toolbox/config-loader.js) — `resolveField()` and `resolveModuleField()` functions
