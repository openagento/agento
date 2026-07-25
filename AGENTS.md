# Agento — AI Agent Framework

Automates Jira tasks using AI agents (Claude Code, OpenAI Codex) in Docker containers with Magento-inspired modular architecture.

## Core Principles

1. **Simplicity over complexity.** Simplest solution that works is the best. Three similar lines > premature abstraction.
2. **Encapsulation, SOLID, DRY.** Clear boundaries. Dependencies through protocols, not concretes. Testable in isolation.
3. **TDD where possible.** Red → green → refactor. Unit tests with mocks (respx/pytest).
4. **Surgical changes.** Only what's necessary. No extra comments, docstrings, type hints in untouched code.
5. Utilize Framework features for new implementations:
    - Event-observers
    - 3-level system config fallback
    - (more in docs/)
6. **Framework is agent-agnostic.** A new agent (OpenCode, Hermes, etc.) must be added without editing framework code — framework defines protocols (Runner, ConfigWriter, AuthStrategy, CliInvoker), agent modules provide implementations and register them via `di.json`. No `if provider == "claude"` branches, no hardcoded `.claude.json` / `.codex/config.toml` logic in `src/agento/framework/`, and no hardcoded CLI flag lists for `claude`/`codex` in `agento run`.

## Key Conventions

- **Python:** httpx (not requests), dataclasses (not Pydantic), PyMySQL (not mysql-connector)
- **Tests:** pytest + respx, fixtures in `tests/fixtures/`
- **CLI:** `bin/agento <command>` — Magento-like CLI
- **Core modules:** `src/agento/modules/<name>/` with `module.json` — ship with framework
- **User modules:** `app/code/<name>/` with `module.json` + `config.json` — per-deployment, gitignored
- **Module dependencies (`sequence`):** If a module imports classes/functions from another module, it **must** declare that module in `sequence`. Prefer using framework code (`src/agento/framework/`) + events/observers over inter-module imports — framework code requires no `sequence` entry. If inter-module dependency is unavoidable (e.g., `jira_periodic_tasks` → `jira`), declare it in `sequence`. Every module must be safely disableable: disabling a module (and its dependents down the chain) must leave the system fully operational.
- **Config:** 3-level fallback: ENV (`CONFIG__MODULE__PATH`) → DB (`core_config_data`) → `config.json`. Per-agent_view scoped config via `scope='agent_view'` in DB.
- **Concurrent execution:** `AGENTO_CONSUMER_MAX_WORKERS` env var (default 1). Per-run isolation makes it safe to increase.
- **Consumer hot-reload:** every `AGENTO_CONSUMER_POLL_INTERVAL` (5s default) the consumer re-runs `bootstrap()` when idle — `mo:en/mo:di`, `config:set`, and `app/code/` edits apply live without restart. Caveat: edits to core module Python code (`src/agento/modules/`) still require a process restart due to `sys.modules` caching.
- **Cron container env contract:** Any env var the cron/consumer needs from `docker-compose` must use the `AGENTO_*` prefix (e.g. `AGENTO_CONSUMER_MAX_WORKERS`). The entrypoint whitelist only persists `AGENTO_*`, `MYSQL_*`, `CONFIG__*`, `TZ`, `PYTHONPATH`, `PROVIDER`, `DISABLE_LLM`, and `DISABLE_AUTOUPDATER` across the `su - agent` env wipe — non-prefixed framework knobs are silently dropped. See [docs/architecture/cron-env-contract.md](docs/architecture/cron-env-contract.md).
- **Routing:** Ingress identities map inbound requests to agent_views. Channels auto-resolve via `resolve_agent_view()` before publishing. The **Outlook** channel routes by mailbox→agent_view: a mailbox UPN owned by exactly one view is **direct mode** (the mailbox identifies the view); a UPN **shared by ≥2 views** is **routed mode** — polled once and each message routed to a view by matching the normalized sender against `outlook_sender` ingress bindings (regex `fullmatch`, highest `--priority` wins; a tie between different views is ambiguous → no job). See [docs/modules/outlook.md](docs/modules/outlook.md). Outlook reads are additionally bound to the triggering job's own message (privacy by construction), and the mailbox-enumeration tools (`outlook_search_messages`/`outlook_get_new_messages`) were removed.
- **Agent view config:** Scoped DB paths `agent_view/provider`, `agent_view/model`, `agent_view/scheduling/priority`, `agent_view/instructions/agents_md`, `agent_view/instructions/soul_md` — resolved with agent_view → workspace → global fallback.
- **Security:** Toolbox = only container with secrets. Agent has NO credentials. Tools and skills are **opt-in** (disabled by default) — least privilege: a tool/skill is available only when `is_enabled` resolves to `1` for the scope (agent_view > workspace > default). Adding a module or syncing a skill grants no access until explicitly enabled.
- **DB tables:** singular names (e.g., `job`, `schedule`, `oauth_token`). Exception: `core_config_data` (Magento convention).
- **Setup:** `setup:upgrade` on deploy — **validates enabled module manifests first** (aborts before any DB change if a manifest is invalid, e.g. a tool missing `toolset`), then applies schema migrations, data patches, installs crontab, runs module onboarding (strict: complete, disable+dependents, or quit). Use `--skip-onboarding` for CI/CD. `bin/test` runs the same `module:validate` check. Manual alternative: pre-set config values via `config:set`. See [docs/cli/onboarding.md](docs/cli/onboarding.md).
- **Module setup files:** `sql/*.sql` (schema migrations), `data_patch.json` (data patches), `cron.json` (cron jobs), `di.json` onboarding (interactive external system setup)
- **Migration tracking:** `schema_migration` table (with `module` column), `data_patch` table
- **Events:** Naming convention: `{subject}_{verb}_{before|after}` — e.g. `job_claim_after`, `module_register_before`, `workspace_build_complete_after`. Third-party: `{vendor}_{module}_{subject}_{verb}_{before|after}`. Prefer `_after` events; use `_before` only when observers need pre-action state. See [docs/architecture/events.md](docs/architecture/events.md).
- **Interactive prompts:** Always use `terminal.select()` (arrow-key selection) for user choices. Never use Y/n text prompts. For text input (paths, port numbers), use `input()` with defaults shown in brackets.
- **Logs:** consumer → JSON structured, publisher/sync → text. Never delete while consumer runs.
- **Code via volume mounts (Magento-like distribution)** — every project owns a `pyproject.toml` (composer.json equivalent) pinned to `agento-core==X.Y.Z` and a per-project `.venv/` (`vendor/` equivalent). Containers bind-mount `<project>/.venv/lib/python3.12/site-packages/agento` (read-only) into `/opt/agento-src/agento`, so editing source on the host + restarting the container = instant effect (no rebuild). Native deps (cryptography, etc.) live in a container-side venv built from the project's `uv.lock`. Customer images are built locally by `agento install`/`upgrade` from the in-package context at `src/agento/framework/docker/` — no GHCR pulls. Dev compose (`docker/docker-compose.dev.yml`) uses the same thin Dockerfiles with a different build context (repo root). After source changes: `cd docker && docker compose -f docker-compose.dev.yml restart cron` (Python) or `… restart toolbox` (JS). Rebuild only for dependency changes (`pyproject.toml` / `package.json`).
- **Docker Compose split** — `docker/docker-compose.yml` is managed (regenerated on `install`/`upgrade`/`module:enable`, DO NOT EDIT). `docker/docker-compose.override.yml` is user-owned — Docker Compose auto-merges both. See [docs/deployment/docker-compose-override.md](docs/deployment/docker-compose-override.md).
- **Upgrade:** `agento upgrade` upgrades the CLI package, bumps `agento-core` in the project's `pyproject.toml`, runs `uv sync`, refreshes `.agento/docker/` build context + `docker-compose.yml`, and rebuilds local Docker images. Use `agento upgrade --version X.Y.Z` to pin a specific version, `--no-build` to skip image rebuild (CI), `--no-restart` to skip `up -d`.
- **Extensions** — three sources, all gated through `app/etc/modules.json`: (1) PyPI marketplace via `uv add <pkg>` then `agento module:enable <pkg>` — auto-resolves via `.venv/site-packages/<pkg>/`, regenerates compose with the new mount, restarts containers; (2) local under `app/code/<vendor>/<name>/module.json` — already mounted via `app/code:ro`; (3) drop a vendored copy into `app/code/`. Local always shadows PyPI of the same name.

## Essential Commands

```bash
# Tests (all: JSON validation + Python + JS)
bin/test

# Or individually:
uv run pytest -q                                       # Python (~756 tests, from repo root)
cd src/agento/toolbox && npm test && cd -              # JS (vitest, from repo root)

# Project lifecycle
agento doctor                                          # Check prerequisites
agento install                                         # Interactive project installation wizard (reinstalls if already installed)
agento upgrade [--version X.Y.Z]                       # Upgrade CLI + Docker images (latest or specific version)
agento up                                              # Start Docker Compose
agento down                                            # Stop containers
agento logs [service]                                  # View container logs
agento admin                                           # Launch admin TUI (runs inside Docker)
agento run <agent_view_code>                           # Interactive agent CLI in sandbox (TTY) — CLI command is supplied by the provider's registered CliInvoker (framework stays agent-agnostic)
agento run <agent_view_code> "<prompt>"                 # Headless one-shot with the given prompt; propagates agent exit code

# Restart after code changes (dev compose)
cd docker && docker compose -f docker-compose.dev.yml restart cron toolbox

# Full rebuild (dependency changes only)
cd docker && docker compose -f docker-compose.dev.yml build cron toolbox && docker compose -f docker-compose.dev.yml up -d --force-recreate

# Setup (after module changes or deploy)
agento setup:upgrade                                   # Apply migrations, data patches, install crontab, run onboarding
agento setup:upgrade --dry-run                         # Preview pending work
agento setup:upgrade --skip-onboarding                 # Skip interactive module onboarding (for CI/CD)

# Modules
agento module:add <name> --tool mysql:<tool_name>:<description>
agento module:list                                     # List all modules with enabled/disabled status
agento module:enable <name>                            # Enable a module (stored in app/etc/modules.json)
agento module:disable <name>                           # Disable a module (skips loading, cron, config, CLI)
agento module:validate [name]                          # Validate module structure and sequence deps

# Jobs
agento job:list [--status DEAD] [--source S] [--agent-view C] [--limit N]  # List recent jobs; surface failed/dead + their error
agento job:pause <job_id>                              # Stop a running job, keep session
agento job:resume <job_id>                             # Re-queue paused job; auto-resumes via session_id

# Config
agento config:set <path> <value> [--scope=<scope>] [--scope-id=<id>]
agento config:get <path>                               # exact path: per-scope values
agento config:get <module>                             # module prefix: tree view by scope
agento config:list [prefix]
agento config:remove <path> [--scope=<scope>] [--scope-id=<id>]
agento config:schema [module] [--json]                 # Show config field definitions from system.json
agento config:resolve <module> [--scope=S] [--scope-id=N] [--json]  # Resolve effective config values with source info

# Tokens (LRU pool per provider — no sticky primary)
agento token:list                                      # status, last_used, expires_at per row
# Register tokens. --with-api-key / --with-access-token are boolean switches; the
# secret is read from stdin (piped) or via interactive getpass prompt (TTY).
# Inline values like `--with-api-key sk-XXX` are REJECTED — they leak through
# shell history, ps, and CI logs. See docs/cli/tokens.md for details.
agento token:register <agent_type> <label>                                  # interactive OAuth
agento token:register codex  <label> --with-api-key                         # TTY: prompts (hidden)
echo "$OPENAI_API_KEY"      | agento token:register codex  <label> --with-api-key
echo "$CODEX_ACCESS_TOKEN"  | agento token:register codex  <label> --with-access-token
echo "$ANTHROPIC_API_KEY"   | agento token:register claude <label> --with-api-key
agento token:register codex  <label> --with-api-key < /path/to/key.txt      # file redirect
agento token:set-priority <token_id> <priority>                             # lower priority wins
agento token:refresh <id>                              # re-auth an existing token
agento token:mark-error <id> "<msg>"                   # quarantine a token (status=error)
agento token:reset <id>                                # clear error status without re-auth

# Ingress identity binding (route inbound requests to agent_views)
agento ingress:bind <type> <value> <agent_view_code> [--priority N]   # e.g. ingress:bind jira jira developer
# Regex types (e.g. outlook_sender for a shared mailbox): <value> is a case-insensitive fullmatch
# regex; --priority selects the winner (higher wins; ties between different views are ambiguous):
agento ingress:bind outlook_sender '[^@]+@company\.com' sales --priority 10
agento ingress:list [--type <type>] [--json]
agento ingress:unbind <type> <value>

# Tools (OPT-IN: disabled by default; enabled only when is_enabled resolves to '1' — toolbox isToolEnabled)
agento tool:list [--agent-view <code>]                 # List tools with enabled/disabled status
agento tool:enable <name> [--agent-view <code>]        # Enable a tool (also: --scope/--scope-id)
agento tool:disable <name> [--agent-view <code>]       # Disable a tool (also: --scope/--scope-id)

# Skills (OPT-IN: disabled by default; enabled only when is_enabled resolves to '1')
agento skill:sync                                      # Scan skills from disk → sync to DB registry
agento skill:list [--agent-view <code>]                # List skills with enabled/disabled status
agento skill:enable <name> [--agent-view <code>]       # Enable a skill (also: --scope/--scope-id)
agento skill:disable <name> [--agent-view <code>]      # Disable a skill (also: --scope/--scope-id)

# Workspace builds (materialized config per agent_view)
agento workspace:build --agent-view <code> [--force]   # Build workspace for one agent_view (--force rebuilds even if unchanged)
agento workspace:build --all [--force]                 # Build for all active agent_views
agento workspace:build-status [--agent-view <code>]    # Show build status
```

## Documentation

Full developer documentation in [docs/](docs/):

- [Getting Started](docs/getting-started.md) — install + first module in 5 minutes
- [CLI Reference](docs/cli/) — all `agento` commands
- [Modules Guide](docs/modules/) — creating and managing modules
- [Config System](docs/config/) — 3-level fallback, encryption, ENV vars
- [Tool Adapters](docs/tools/) — built-in + creating custom adapters
- [Architecture](docs/architecture/) — containers, zero-trust, job queue

## Strategic Decisions

Architectural and technical decisions (why httpx, why PyMySQL, idempotency design, etc.) are documented in [DECISIONS.md](DECISIONS.md). Add new decisions there when making non-obvious technical choices.

## Additional References

- [docker/README.md](docker/README.md) — Docker deployment, auth, Playwright setup
- [docker/cron/app/README.md](docker/cron/app/README.md) — Docker cron container internals
- [ROADMAP.md](ROADMAP.md) — framework evolution roadmap
