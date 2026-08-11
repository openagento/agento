# Agento
**Security by design, not by prompt.**

[![CI](https://github.com/agento-cc/agento/actions/workflows/ci.yml/badge.svg)](https://github.com/agento-cc/agento/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Stop running agents in YOLO mode. Build them with isolation, policy, and modular control.

Agento is an open-source, self-hosted platform for building modular agentic software with **hard runtime boundaries, controlled tool access, secure secrets handling, and deployment-specific extensibility**.

Modern agent stacks are powerful, but in practice they are often glued together from prompts, scripts, MCP servers, and broad permissions. The result is fragile and risky: duplicated files, unclear tool access policies, missing human approval steps, over-permissioned agents, and no clean way to route the right task to the right agent and model.

Agento is extensible by design. Creating and sharing custom modules is simple. Want to distribute your QA agent as a reusable template? Package it as a self-contained module and share it with your team, your clients, or the community.

## Why Agento?

- **Secure by architecture** — agents run in an isolated sandbox without direct access to secrets.
- **Controlled tool access** — enforce policies for tools like email, browser, and external systems.
- **Modular by default** — extend behavior through modules, not by patching core code.
- **Deployment-specific customization** — adapt agents, policies, and workflows per workspace or environment.
- **Routing-ready** — decide which agent, model, and tool policy should handle each task.
- **Built for self-hosting** — keep control over your infrastructure, credentials, and runtime boundaries.

## The problem

Teams adopting AI agents quickly run into the same issues:

- too many separate agents with duplicated prompt and config files,
- unclear separation between runtime, tools, and secrets,
- agents operating with permissions that are too broad,
- missing or weak HITL / approval flows,
- no enforceable policy layer for actions like sending email or browsing the web,
- no clean routing layer for deciding which task should go to which agent and model.

We keep hearing the same failure stories: deleted workspaces, leaked emails, agents browsing beyond intended domains, and automations acting with more access than they should ever have had.

## The solution

Agento brings structure, security, and extensibility to agentic systems through:

- **task routing**
- **runtime isolation**
- **tool access policies**
- **secrets separation**
- **filesystem and environment separation**
- **custom MCP-based security proxy**
- **module-driven extensibility**

## Inspiration

Agento is inspired by **Magento Open Source** — especially its extensibility, extension-first architecture, and strong community model — but rethought for the age of AI agents, MCP tools, and secure autonomous workflows.

## Quick Start

```bash
uv tool install agento-core           # Install the CLI
mkdir my-project && cd my-project
agento install                        # Interactive wizard — scaffolds, starts, migrates
```

## Architecture

Agento runs three Docker containers on a shared network:

- **Cron** (Python) -- Job queue consumer, scheduler, CLI host. Manages the lifecycle of agent jobs, runs migrations, and dispatches events. Connects to MySQL for job state, config, and module metadata.
- **Toolbox** (Node.js) -- MCP credential broker. Registers tools from modules (MySQL adapters, API clients) and exposes them over stdio. The only container with access to secrets.
- **Sandbox** (Claude Code / OpenAI Codex) -- Ephemeral container where the AI agent executes. Has no credentials, no direct database access. Communicates with the toolbox exclusively through MCP tool calls.

## Module System

Agento uses a Magento-inspired modular architecture. Each module is a self-contained package.

**Core modules** ship with the framework in `src/agento/modules/` (jira, claude, codex, core, crypt, agent_view).

**User modules** live in `app/code/` and are deployment-specific (gitignored by default).

Every module contains a `module.json` manifest and optional companion files:

| File | Purpose |
|------|---------|
| `module.json` | Module manifest (name, version, tools, knowledge) |
| `di.json` | Dependency injection configuration |
| `events.json` | Observer declarations for event-driven extensibility |
| `config.json` | Default config values with field metadata |
| `cron.json` | Scheduled job definitions |
| `sql/*.sql` | Schema migrations |
| `data_patch.json` | Data patches applied during setup |

**Config** follows a 3-level fallback: ENV vars (`CONFIG__MODULE__PATH`) take highest priority, then DB (`core_config_data`), then `config.json` defaults. Config can be scoped per agent_view for multi-tenant setups.

**Events** use an observer pattern. Modules declare observers in `events.json` and the framework dispatches events synchronously during lifecycle hooks (job start, job complete, schedule tick, etc.).

## Installation

### Docker Compose (recommended)

For end users, demos, PoC, and self-hosting:

```bash
uv tool install agento-core          # or: pip install agento-core
mkdir my-project && cd my-project
agento install                        # Interactive wizard — scaffolds, starts, migrates
```

The installer offers **Basic** (recommended) and **Advanced** modes. Basic uses sensible defaults. Advanced lets you configure Docker project name, MySQL port, and timezone for multi-instance setups.

### System check

```bash
agento doctor                         # Verify prerequisites
```

## Admin TUI

`agento admin` launches an interactive terminal dashboard for operational visibility and configuration management.

![Admin TUI](docs/images/admin_config.jpg)

- **Dashboard** -- system health, recent jobs, credentials, agent views at a glance
- **Jobs** -- browse, filter, search, view details, replay jobs
- **Agents** -- manage agent views, trigger workspace builds
- **Credentials** -- usage stats, clear error state, deregister
- **Config** -- schema-driven editor with scope selector and live search

Keyboard-first with full mouse support. See [Admin TUI docs](docs/cli/admin.md) for details.

## Creating Your First Module

```bash
agento module:add my-app \
  --description="My application module" \
  --tool mysql:mysql_prod:"Production database (read-only)"
```

This creates a module in `app/code/my-app/` with a `module.json`, `config.json`, and `knowledge/` directory. Set credentials with:

```bash
agento config:set my_app/tools/mysql_prod/host 10.0.0.1
agento config:set my_app/tools/mysql_prod/pass secret123
```

See [Creating a Module](docs/modules/creating-a-module.md) for the full guide.

## Documentation

Full developer documentation is available in [docs/](docs/):

- [Getting Started](docs/getting-started.md) -- Install and create your first module
- [CLI Reference](docs/cli/) -- All `agento` commands
- [Module Guide](docs/modules/) -- Creating and managing modules
- [Config System](docs/config/) -- 3-level fallback, encryption, ENV vars
- [Architecture](docs/architecture/) -- Containers, zero-trust, job queue

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on setting up a development environment, running tests, and submitting pull requests.

## License

MIT. See [LICENSE](LICENSE) for the full text.
