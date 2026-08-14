# Modules

A module is a self-contained package — like a Magento module in `app/code/`. One module = one integration = complete package.

## Directory Structure

```
modules/
  my-ecommerce/
    module.json       # Manifest: metadata, tools, field schemas
    config.json       # Default config values (non-secret)
    di.json           # Capability bindings: channels, workflows, commands
    system.json       # Config field schemas with types and labels
    events.json       # Event observer declarations
    data_patch.json   # Data patch declarations
    cron.json         # Cron job declarations
    sql/              # Schema migrations
    src/              # Python code (channels, workflows, commands, observers)
    knowledge/        # Documentation for the AI agent
      README.md       # System overview
      DB_SCHEMA.md    # Database docs
      incidents.md    # Past incidents
    prompts/          # Diagnostic methodologies
    skills/           # Agent capabilities
  _example/           # Example module (shipped with repo, not loaded)
```

## What Each Part Does

| Directory | Purpose | Used By |
|-----------|---------|---------|
| `module.json` | Tool definitions with field schemas | Toolbox (registers MCP tools) |
| `di.json` | Channel, workflow, runtime, onboarding class bindings | Bootstrap (populates registries) |
| `system.json` | Config field schemas with types and labels | Config resolver (3-level fallback) |
| `events.json` | Event observer declarations | Bootstrap (wires observers) |
| `config.json` | Non-secret default values (hosts, ports, DB names) | Toolbox (config fallback) |
| `data_patch.json` | Data patch declarations (seeding, transforms) | Setup (`setup:upgrade`) |
| `cron.json` | Cron job declarations (scheduled CLI commands) | Setup (`setup:upgrade`) |
| `sql/` | Schema migrations (numbered SQL files) | Setup (`setup:upgrade`) |
| `knowledge/` | System documentation for the AI agent | Agent (reads during tasks) |
| `prompts/` | Step-by-step diagnostic methodologies | Agent (follows during diagnosis) |
| `skills/` | Reusable task templates | Agent (executes on request) |

## Module Loading

Toolbox reads `module.json` + `config.json` directly from `/modules/` (mounted read-only in Docker).

Agent reads module content from per-agent_view workspace builds (`workspace/build/`), materialized by `workspace:build`. Each module's `workspace/` directory is compiled into `build/{ws}/{av}/modules/{name}/`.

## Quick Start

```bash
bin/agento module:add my-system --tool mysql:mysql_my_prod:"My Production DB"
# Omit the value → agento prompts, paste, Ctrl+D. Keeps the secret out of bash history.
bin/agento config:set my_system/tools/mysql_my_prod/pass
```

## Further Reading

- [module.json format](module-json.md)
- [config.json format](config-json.md)
- [Creating a module step-by-step](creating-a-module.md)

### Bundled channel modules

- [Outlook / Microsoft 365 email channel](outlook.md) — DMARC-gated, allow-listed inbound email channel
- [Bitbucket Cloud PR-review channel](bitbucket.md) — watches an agent's open PRs and queues review work
- [GitHub PR-review channel](github.md) — watches an agent's open GitHub PRs and queues review work
