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

Agento runs six service containers plus MySQL. All but one share the `agento-net` bridge
network — the **Artifacts** container deliberately joins none of it:

- **Cron** (Python) -- Job queue consumer, scheduler, CLI host. Manages the lifecycle of agent jobs, runs migrations, and dispatches events. Connects to MySQL for job state, config, and module metadata.
- **Toolbox** (Node.js) -- MCP credential broker. Registers tools from modules (MySQL adapters, API clients) and exposes them over MCP (streamable HTTP `/mcp`, SSE `/sse`). Designed to be the only container that holds tool credentials (known gaps: [zero-trust](docs/architecture/zero-trust.md#known-exceptions-and-debt)).
- **Sandbox** (Claude Code / OpenAI Codex / Pi -- the set is open, see the harness contract) -- Ephemeral container for interactive `agento run`. Holds no upstream service credentials and no direct database access — only its own run's toolbox capability, scoped to one agent_view and expiring. Headless jobs run the agent inside the `cron` container and inherit its env (a known gap, see [zero-trust](docs/architecture/zero-trust.md#known-exceptions-and-debt)). Communicates with the toolbox exclusively through MCP tool calls.
- **Artifacts** (Node.js) -- Static HTTP for the `versioned_artifacts` published tree. On no shared network, holding no secret and publishing no host port: `proxy` is the only route to its files.
- **Web** (Python) -- The panel API: sign-in, per-role grants, admin, artifact launches. Holds only the internal proxy secret (no upstream tool credential, no encryption key) and never decrypts config; it stores only hashes of session and launch tokens. See [docs/architecture/panel.md](docs/architecture/panel.md).
- **Proxy** (Caddy) -- TLS and the panel / apps / share origins; asks `web` to authorize every artifact file request.

## Module System

Agento uses a Magento-inspired modular architecture. Each module is a self-contained package.

**Core modules** ship with the framework in `src/agento/modules/` (jira, claude, codex, pi, core, crypt, agent_view, versioned_artifacts, web).

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

## Roadmap

One module = one integration = a complete package. The framework provides the mechanics, Core defines meaning, and Modules deliver features — every milestone stays backward-compatible and keeps the Python/Node.js security boundary intact.

- ✅ Magento-style module system (manifests, 3-level config, dynamic tool loading)
- ✅ Core contracts & module-driven registries (channels, workflows, runtimes)
- ✅ Framework kernel & scoped configuration (per-module config, deterministic load order)
- ✅ Event–observer system (`events.json`, cross-module composition)
- ✅ Core module refactoring (framework has zero module imports)
- ✅ Module setup system (`setup:upgrade` — migrations, data patches, cron)
- ✅ Workspace & agent-view hierarchy (scoped config, generated CLI configs)
- ✅ Concurrent agent-view execution pool (parallel profiles, priority scheduling)
- ✅ Ingress identities & agent resolution (deterministic, module-extensible routing)
- ✅ Composable workspace, skills & tools (CLI-managed tool/skill control)
- 🟡 Developer experience & open-source polish (docs, CI boundary tests)
- 🟡 Event coverage & naming convention (`{subject}_{verb}_{before|after}`)
- 🟡 Composable workspace automation (auto-rebuild, build GC — scheduled sync pending)
- ⚪ Admin API & Agent Studio (control plane for workspaces & agent views)
- ⚪ Credential broker / key vault (broker-owned secrets, reference-based config)
- ⚪ Response locale policy (per-scope output language)
- ⚪ OAuth token pools (capacity-based rotation, per-agent-view assignment)
- ⚪ Distribution & installation model (pre-built images, no local build)
- ⚪ Observability, telemetry & evals (OpenTelemetry traces, eval datasets, regression gates)
- ⚪ Dynamic harness/model/effort routing (rule-based pre-execution task assessment)
- ⚪ Job threads, handoffs & shared artifacts (grouped jobs, agent-to-agent handoff packages)
- ⚪ Areas / selective module loading (parked)
- ⚪ Declarative schema `db_schema.json` (deferred)

Legend: ✅ shipped · 🟡 in progress · ⚪ planned. This is the short preview — see the full roadmap in [ROADMAP.md](ROADMAP.md).

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on setting up a development environment, running tests, and submitting pull requests.

## License

MIT. See [LICENSE](LICENSE) for the full text.
