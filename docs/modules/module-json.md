# module.json

Module manifest — declares metadata, tools, and field schemas. Read by Toolbox at startup.

## Full Schema

```json
{
  "name": "my-ecommerce",
  "version": "1.0.0",
  "description": "My e-commerce platform",
  "repo": "git@github.com:org/my-ecommerce.git",
  "tools": [
    {
      "type": "mysql",
      "name": "mysql_ecom_prod",
      "description": "Production MySQL (read-only). Tables: orders, products, customers.",
      "fields": {
        "host":     {"type": "string",  "label": "Host"},
        "port":     {"type": "integer", "label": "Port", "default": 3306},
        "user":     {"type": "string",  "label": "User"},
        "pass":     {"type": "obscure", "label": "Password"},
        "database": {"type": "string",  "label": "Database"}
      }
    }
  ],
  "log_servers": [
    {
      "name": "app-server",
      "host": "log_reader@app.example.com",
      "apps": ["my-ecommerce", "redis"]
    }
  ]
}
```

## Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Module identifier (used in config paths, directory name) |
| `version` | No | Semantic version |
| `description` | Yes | Human-readable description (shown in `module:list`) |
| `sequence` | No | Array of module names this module depends on (Magento `<sequence>`). Dependencies load first. Default: `[]` |
| `order` | No | Integer sort position within same dependency tier — lower loads earlier. Default: `1000` |
| `repo` | No | Git repository URL for source code access |
| `tools` | No | Array of tool definitions |
| `log_servers` | No | SSH log servers for the AI agent |

## Tool Definition

| Field | Description |
|-------|-------------|
| `type` | Adapter type: `mysql`, `mssql`, `opensearch` |
| `name` | Globally unique tool name (registered as MCP tool) |
| `description` | Shown to the AI agent — include table names, data types |
| `fields` | Config field schemas (see below) |

## Field Types

| Type | Description | Encrypted in DB? |
|------|-------------|-------------------|
| `string` | Plain text value | No |
| `integer` | Numeric value | No |
| `obscure` | Sensitive value (password, token) | Yes (AES-256-CBC) |

Fields with `"default"` are used as the lowest-priority fallback if no ENV, DB, or config.json value exists.

## Companion Files

Magento-style separation — each concern has its own JSON file:

| File | Purpose | Magento Equivalent |
|------|---------|-------------------|
| `di.json` | Channel, workflow, runtime class bindings | `di.xml` |
| `system.json` | Config field schemas with types and labels | `system.xml` |
| `events.json` | Event observer declarations | `events.xml` |
| `config.json` | Default config values (non-secret) | `config.xml` |
| `data_patch.json` | Data patch declarations (seeding, data transforms) | `DataPatchInterface` |
| `cron.json` | Cron job declarations (scheduled CLI commands) | `crontab.xml` |
| `sql/*.sql` | Schema migrations (numbered, sequential) | `db_schema.xml` / setup scripts |

### toolbox/ directory

JS files in `toolbox/` are auto-discovered by the Toolbox container at startup. Each file must export `register(server, context)`. The `context` provides `{ app, log, db, playwright }`.

```
my-module/
  toolbox/
    my-tool.js      # exports register(server, context) — auto-discovered
    api.js           # REST API routes — also auto-discovered
```

See [src/agento/modules/jira/toolbox/](../../src/agento/modules/jira/toolbox/) for a complete example with MCP tools and REST routes.

### di.json

Registers capabilities your module provides — channels, workflows, runtimes, CLI commands. Bootstrap reads this and populates the corresponding registries.

```json
{
  "channels": [
    {"name": "jira", "class": "src.channel.JiraChannel"}
  ],
  "workflows": [
    {"type": "cron", "class": "src.workflows.cron.CronWorkflow"},
    {"type": "todo", "class": "src.workflows.todo.TodoWorkflow"}
  ],
  "runtimes": [
    {"provider": "claude", "class": "src.runner.TokenClaudeRunner"}
  ],
  "config_writers": [
    {"provider": "claude", "class": "src.config.ClaudeConfigWriter"}
  ],
  "sandbox_packages": [
    {
      "provider": "claude",
      "manager": "npm",
      "package": "@anthropic-ai/claude-code",
      "binary": "claude",
      "version_env_key": "CLAUDE_CODE_VERSION",
      "default_range": "~2.1.142"
    }
  ],
  "commands": [
    {"name": "sync", "class": "src.commands.sync.SyncCommand"},
    {"name": "publish", "class": "src.commands.publish.PublishCommand"}
  ],
  "regex_identity_types": ["outlook_sender"]
}
```

Each section is optional — include only what your module provides. The `onboarding` key is a single class path string (not an array), since a module has at most one onboarding flow.

| Section | Key Fields | Registry |
|---------|-----------|----------|
| `channels` | `name`, `class` | Channel registry — `get_channel(name)` |
| `workflows` | `type`, `class` | Workflow registry — `get_workflow_class(AgentType)` |
| `runtimes` | `provider`, `class` | Runner factory — `create_runner(AgentProvider)` |
| `config_writers` | `provider`, `class` | Config writer registry — `get_config_writer(AgentProvider)` |
| `sandbox_packages` | `provider`, `manager`, `package`, `binary`, `version_env_key`, `default_range` | Sandbox CLI version registry — drives `docker/.env` pin seeding, sandbox `--build-arg`s, and the `agento doctor` pin check |
| `commands` | `name`, `class` | CLI command registry — adds `bin/agento <name>` subcommand |
| `onboarding` | (single class path string) | Onboarding registry — interactive setup during `setup:upgrade` |
| `regex_identity_types` | (array of identity-type strings) | Regex-identity-type registry — `is_regex_identity_type(type)` |

#### `regex_identity_types`

An array of ingress `identity_type` strings whose bindings are matched by **case-insensitive
regex `fullmatch`** (highest `priority` wins) instead of exact string equality. The framework
registry starts empty and is populated at bootstrap from each module's declaration, so the
framework hardcodes no channel-specific type — the owning module declares its own, and disabling
the module drops it. Each entry must match `^[a-z][a-z0-9_]{0,31}$` (fits `identity_type
VARCHAR(32)`). Both the runtime matcher and the `ingress:bind` CLI validator gate on the same
`is_regex_identity_type(...)` predicate. Example: the `outlook` module declares
`["outlook_sender"]` so a shared mailbox can route inbound mail to different agent_views by sender
pattern. See [routing](../architecture/routing.md).

#### `sandbox_packages`

Declares the CLI binary your agent module needs installed in the sandbox image, plus a soft version pin. The framework enumerates these declarations at install/upgrade/doctor time — no framework code branches on provider name.

| Field | Required | Description |
|---|---|---|
| `provider` | yes | Stable identifier, matches `runtimes.provider` for the same agent |
| `manager` | yes | Package manager (`"npm"` today; shape future-proofs apt/pip) |
| `package` | yes | Full package spec (e.g. `@anthropic-ai/claude-code`) |
| `binary` | yes | CLI binary name on `$PATH` — used for `<binary> --version` doctor checks and the doctor row label |
| `version_env_key` | yes | UPPERCASE name of the env var seeded in `docker/.env` and consumed by the sandbox Dockerfile `ARG`. Must be unique across all modules |
| `default_range` | yes | npm-style semver range (tilde recommended), used when the customer hasn't set the env var |

The current sandbox Dockerfile hardcodes `RUN npm install -g @anthropic-ai/claude-code @openai/codex`, so a third-party agent module needs that line extended too until the Dockerfile becomes data-driven. The registry contract for pin propagation is in place today.

#### `config_writers`

Each agent provider materializes its own CLI config files (`.claude.json`, `.codex/config.toml`, etc.) before a job runs. The framework dispatches to the provider's `ConfigWriter` — it does **not** know about specific file formats. To add a new agent (OpenCode, Hermes, etc.), implement `ConfigWriter` and register it here.

The protocol (`src/agento/framework/config_writer.py`):

```python
class ConfigWriter(Protocol):
    def prepare_workspace(self, working_dir, agent_config, *, agent_view_id=None) -> None: ...
    def inject_runtime_params(self, artifacts_dir, *, job_id) -> None: ...
    def owned_paths(self) -> tuple[set[str], set[str]]: ...
```

- `prepare_workspace` — write provider config files into the build directory (model, MCP servers, permissions, etc.)
- `inject_runtime_params` — append the per-job `job_id` to MCP URLs in the artifacts copy of the config (workspace/agent_view codes are resolved toolbox-side from `agent_view_id`)
- `owned_paths` — return `(files, dirs)` this writer manages so framework copies (not symlinks) them into per-job artifacts dirs

Class paths are dotted relative to the module directory: `src.channel.JiraChannel` resolves to `<module>/src/channel.py` → `JiraChannel`.

### system.json

Declares config fields your module needs, with types and labels. The framework resolves values through the [3-level fallback](../config/README.md) (ENV → DB → config.json). Default values belong in `config.json`.

```json
{
  "base_url": {"type": "string", "label": "Service base URL"},
  "user": {"type": "string", "label": "AI User"},
  "api_token": {"type": "obscure", "label": "API Token"},
  "max_results": {"type": "integer", "label": "Max results"},
  "project_list": {"type": "json", "label": "Projects (JSON array)"},
  "log_level": {
    "type": "select",
    "label": "Logging level",
    "options": [
      {"value": "debug", "label": "Debug"},
      {"value": "info", "label": "Info (default)"},
      {"value": "warning", "label": "Warning"},
      {"value": "error", "label": "Error"}
    ]
  }
}
```

Field types: `string`, `integer`, `boolean`, `json`, `obscure` (encrypted in DB), `select` (dropdown with predefined options), `multiselect`.

Fields of type `select` or `multiselect` require an `options` array — each entry has `value` (stored in DB) and `label` (shown in admin UI). The `config:set` CLI and admin editor validate values against the allowed options.

Resolved config is available at runtime via `get_module_config("module_name")` — returns a `dict[str, Any]`.

### events.json

Declares observers that react to framework events. See [Event-Observer System](../architecture/events.md) for full event list.

```json
{
  "job_failed": [
    {
      "name": "mymodule_job_failed",
      "class": "src.observers.JobFailedObserver",
      "order": 100
    }
  ]
}
```

Observer classes must implement `execute(event)`:

```python
class JobFailedObserver:
    def execute(self, event):
        # event.job, event.error, event.elapsed_ms
        logger.warning("Job %d failed: %s", event.job.id, event.error)
```

### data_patch.json

Declares data patches — Python classes that seed or transform data. Applied by `setup:upgrade` in topological order (respecting `require()` dependencies). Tracked in the `data_patch` table.

```json
{
    "patches": [
        {"name": "SeedDefaults", "class": "src.patches.seed_defaults.SeedDefaults"}
    ]
}
```

Patch classes implement the `DataPatch` protocol (import from `agento.framework.contracts`):

```python
from agento.framework.contracts import DataPatch

class SeedDefaults:
    def apply(self, conn):
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO config ...")
        conn.commit()

    def require(self):
        # Fully-qualified names: "module/PatchName"
        return []  # No dependencies
```

`require()` returns a list of patch names that must run first (Magento's `getDependencies()`). Names use `module/PatchName` format.

### cron.json

Declares scheduled CLI commands. Collected by `setup:upgrade` into the `AGENTO:BEGIN/END` crontab block. Separate from dynamic cron blocks (e.g. Jira's `JIRA-SYNC:BEGIN/END` for per-issue entries).

```json
{
    "jobs": [
        {"name": "sync", "schedule": "0 * * * *", "command": "sync"},
        {"name": "publish_todo", "schedule": "* * * * *", "command": "publish jira-todo"}
    ]
}
```

`command` is the CLI subcommand name — the framework wraps it with environment loading and Docker paths. Module authors don't need to know about container internals.

### sql/ directory

Numbered SQL migration files, applied by `setup:upgrade` in module dependency order. Same convention as framework migrations.

```
my-module/
  sql/
    001_create_custom_table.sql
    002_add_index.sql
```

Tracked in the `schema_migration` table with a `module` column distinguishing framework from module migrations.

### CLI Commands

Modules contribute CLI subcommands via the `commands` section in `di.json`. Command classes implement the `Command` protocol:

```python
import argparse

class SyncCommand:
    @property
    def name(self) -> str:
        return "sync"

    @property
    def help(self) -> str:
        return "Sync recurring tasks to crontab"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def execute(self, args: argparse.Namespace) -> None:
        # Your command logic here
        ...
```

After bootstrap, the command appears as `bin/agento sync --dry-run`. Framework commands (consumer, setup:upgrade, config, token) are built-in; module commands extend the CLI dynamically.

### Onboarding

Modules can declare an interactive onboarding flow that runs during `setup:upgrade`. This is useful for modules that need to configure external systems (e.g., create Jira custom fields, set up third-party API resources) before they can work.

Declare an onboarding class in `di.json`:

```json
{
  "onboarding": "src.onboarding.MyOnboarding"
}
```

Unlike other `di.json` sections (which are arrays), `onboarding` is a single class path string — a module has at most one onboarding flow.

Onboarding classes implement the `ModuleOnboarding` protocol:

```python
import pymysql
import logging

class MyOnboarding:
    def is_complete(self, conn: pymysql.Connection) -> bool:
        """Check if onboarding was already completed.
        Typically checks if required config values exist in core_config_data."""
        ...

    def describe(self) -> str:
        """Human-readable one-liner shown when prompting the user."""
        return "Configure external system X for module Y"

    def run(self, conn: pymysql.Connection, config: dict, logger: logging.Logger) -> None:
        """Execute the interactive onboarding flow.
        config is the resolved module config (3-level fallback).
        Use input() for user prompts. Save results to core_config_data."""
        ...
```

**Lifecycle:**
- Runs as step 5 of `setup:upgrade` (after migrations, data patches, and cron)
- Skipped when `is_complete()` returns `True` (idempotent)
- Skipped with `--skip-onboarding` flag (for CI/CD)
- Skipped in `--dry-run` mode
- User is prompted per module before running ("Proceed? [Y/n]")

**Error handling:** Check before creating. On failure, print a clear error and return — the user can fix the issue and re-run `setup:upgrade`.

See `src/agento/modules/jira_periodic_tasks/src/onboarding.py` for a complete example.

## Reference

See [app/code/_example/](../../app/code/_example/) for a working example with observers.

See [src/agento/modules/jira/](../../src/agento/modules/jira/) for a complete core module with channels, workflows, and commands.
