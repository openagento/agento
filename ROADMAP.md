# Agento Roadmap: Framework / Core / Modules

> **Driver:** Open-source readiness — clean extension points, stable contracts, third-party extensibility.
>
> **Principle:** Framework provides mechanics, Core defines meaning, Modules deliver features.
>
> **Design:** One module = one integration = complete package (Magento spirit). A `jira` module provides its channel, workflows, tools, knowledge, and config. The framework merges each declaration into the right registry.
>
> **Security:** Two languages (Python + Node.js) are intentional — the language boundary IS a security boundary. You cannot accidentally mix credential-handling code (Node.js toolbox) with agent execution code (Python cron). This stays.
>
> **Constraint:** Each phase must be backward-compatible. No big-bang rewrite. Existing tests must pass throughout.

---

## Phase 0 — DONE

Magento-like module system fully operational:
- `modules/` directory with `module.json` manifests and `config.json` defaults
- Config-loader (Node.js) with 3-level fallback: ENV → DB → config.json
- `core_config_data` table (Magento-like, scoped)
- Cross-compatible AES-256-CBC encryption (Python + Node.js)
- CLI: `bin/agento install`, `reindex`, `module:add/list/remove`, `config:set/get/list`
- Dynamic tool loading in Toolbox from module manifests

## Phase 1: Core Contracts & Module-Driven Registries — DONE

### Done so far

1. **Module loader** (`module_loader.py`) — scans module directories, reads `module.json` manifests, dynamically imports declared classes via `importlib`, returns `ModuleManifest` dataclasses.

2. **Bootstrap** (`bootstrap.py`) — replaces hardcoded imports. Scans modules, populates all registries (channels, workflows, runtimes) from `module.json` `provides` declarations. `BlankWorkflow` registered as core (not tied to any module).

3. **Core modules extracted** — Jira, Claude, and Codex code moved into self-contained modules with `module.json` manifests:
   - `jira` — channel + 3 workflows (cron, todo, followup)
   - `claude` — runtime (TokenClaudeRunner)
   - `codex` — runtime (TokenCodexRunner)

4. **Core / user module split** (Magento `vendor/` vs `app/code/` model):
   - **Core modules** in `src/agento/modules/` — ship with the framework, tracked in git
   - **User modules** in `app/code/` — gitignored, per-deployment, like Magento `app/code/{Namespace}/{Module}`
   - Bootstrap scans both: core first, user can override

5. **Registries made dynamic** — `channels/registry.py`, `workflows/__init__.py`, `runner_factory.py` now populated from module manifests instead of hardcoded imports. Lazy fallback preserved for backward compatibility during transition.

6. **CLI updated** — `bin/agento module:list` shows core and user modules separately with their provided capabilities.

7. **All 385 tests pass** throughout.

---

### Remaining Phase 1 work

1. ~~**Restructure to standard Python package layout**~~ — DONE. Standard PyPA `src/` layout at repo root.
   - `src/agento/framework/` — kernel (bootstrap, consumer, registries, contracts)
   - `src/agento/modules/` — core modules (jira, claude, codex)
   - `app/code/` — user modules (gitignored, like Magento `app/code/{Namespace}/{Module}`)
   - `tests/` — at repo root
   - `pyproject.toml` — at repo root, package name `agento`
   - `entry_points` for pip-installable third-party modules

2. **Formalize core contracts** — move existing protocols to `framework/contracts/`:
   - `Channel` Protocol (from `channels/base.py`)
   - `DiscoverableChannel` Protocol
   - `Workflow` ABC (from `workflows/base.py`)
   - `Runner` Protocol (from `runner.py`)
   - `Publisher` Protocol (new — extract from `JiraChannel`)
   - Domain models: `Job`, `RunResult`, `WorkItem`, `PromptFragments`

3. **Eliminate consumer dispatch chain** — the `if/elif` dispatch on `AgentType` in `consumer.py` disappears. Each Workflow subclass handles its own argument extraction from Job. Consumer just does: `workflow.execute(channel, job)`.

4. **Separate concerns in JiraChannel** — currently mixes prompt fragments (Channel), job publishing (Publisher), and work discovery (DiscoverableChannel). Split into focused classes, all within the `jira` module.

### Key Technical Decisions Made
- **Module class loading:** `importlib` with dotted path relative to module's `src/` dir. Module dir added to `sys.path` temporarily during import.
- **Blank workflow:** Lives in framework (core), not tied to any module. Registered by bootstrap after module scan.
- **Backward compatibility:** Re-export wrappers from old locations (`channels/registry.py`, `workflows/__init__.py`, `runner_factory.py`) with lazy fallback during transition.
- **Core vs user modules:** Core in `src/agento/modules/` (Magento `vendor/`), user in `app/code/` (Magento `app/code/`). Bootstrap scans both; user can override core.
- **Package layout:** Standard PyPA `src/` layout. Package name `agento`. Third-party modules register via `entry_points["agento.modules"]`.

### Acceptance Criteria

- [x] Python module loader scans `module.json`, dynamically imports classes, populates registries
- [x] The `jira` module provides channel + 3 workflows from one `module.json`
- [x] `claude` and `codex` are modules with their own `module.json`
- [x] All registries (channels, workflows, runtimes) populated from module manifests
- [x] All 385 Python tests pass (backward-compatible re-exports)
- [x] `bin/agento module:list` shows each module with all its provided capabilities
- [x] Standard Python package layout (`src/agento/` at repo root, not `docker/cron/app/src/`)
- [x] Consumer `_run_job()` has no type-specific branching
- [x] JiraChannel concerns separated (Channel, Publisher, DiscoverableChannel)
- [x] Formalized contracts in `framework/contracts/`
- [x] A new integration can be added by creating a module directory — zero changes to framework files
- [x] Removing a module directory cleanly removes its capabilities

---

## Phase 2: Framework Kernel & Scoped Configuration — DONE

### Business Need

`CronConfig` is a god-object with 15+ fields mixing Jira credentials, MySQL connection, consumer tuning, and agent manager config. Adding a new module's config requires editing this frozen dataclass. There's no way for a module to declare "I need these config values" and have them resolved automatically.

For open-source: module authors need to declare their config schema in `module.json` and have it resolved through the existing 3-level fallback (ENV → DB → config.json) without understanding the host application's config plumbing.

The 3-level fallback already works perfectly in Node.js (`config-loader.js`). Python needs the same mechanism.

### Done

1. **Python config-resolver** (`config_resolver.py`) — ports the Node.js `config-loader.js` 3-level fallback to Python:
   - `resolve_field()` / `resolve_tool_field()` with ENV → DB → config.json
   - `ResolvedValue` dataclass tracks value + source for `config:list` display
   - `load_db_overrides()` batch-loads `core_config_data` for efficient resolution
   - Type coercion: string, integer, boolean, json, obscure

2. **Framework config dataclasses** extracted from CronConfig:
   - `DatabaseConfig` — MySQL connection (mysql_host, mysql_port, etc.), duck-typing compatible with `get_connection()`
   - `ConsumerConfig` — concurrency, poll_interval, job_timeout_seconds, disable_llm
   - Both with `from_env()` classmethods

3. **CronConfig decomposed** — now a thin bridge (`from_resolved()`) that delegates to DatabaseConfig + ConsumerConfig + jira module config. Still exists for backward compatibility but all CLI commands route through the new config system via `_load_full_config()`.

4. **Module config declarations** — `module.json` gains `config` section:
   - Jira module declares 8 config fields with types
   - `jira/config.json` holds deployment defaults (toolbox_url, frequency_map)
   - Bootstrap resolves all module configs at startup via `get_module_config(name)`

5. **Dependency ordering** (Magento-style naming):
   - `sequence`: list of module names this module depends on (topological sort)
   - `order`: integer sort position within same tier (lower = earlier)
   - `dependency_resolver.py` with Kahn's algorithm, cycle detection, missing-dep warnings

6. **Bootstrap enhanced** — `bootstrap()` gains `db_conn` param for DB-level config resolution. Module configs stored in `_MODULE_CONFIGS` registry, accessed via `get_module_config()`.

7. **`config:list` upgraded** — shows all module + tool config fields with resolved values and source indicators `[env]`, `[db]`, `[config.json]`.

8. **`config:set` enhanced** — handles module config paths (`{module}/{field}`) in addition to tool paths, auto-detects obscure type for both.

9. **All protocol type hints loosened** — `DiscoverableChannel`, `Publisher`, `Workflow.JobContext` no longer depend on CronConfig.

10. **Consumer internals migrated** — extracts `DatabaseConfig` and `ConsumerConfig` at construction, uses them for all internal operations.

11. **All 448 tests pass** throughout.

### Acceptance Criteria

- [x] CronConfig decomposed — each module has its own config resolved from `module.json` fields. CronConfig is a thin bridge.
- [x] A new module can declare config fields in `module.json` and read them at startup without touching any existing code
- [x] Python config resolution produces identical results to Node.js for the same module/field/fallback chain
- [x] `bin/agento config:list` shows per-module config with resolved values and their source (ENV/DB/config.json/default)
- [x] `bin/agento config:set jira/token <value>` sets an encrypted override in DB (auto-detects `obscure` type)
- [x] Module load order is deterministic and respects `sequence` + `order`
- [x] Consumer, publisher, and CLI all use the new bootstrap sequence
- [x] Framework config (database, consumer) is separate from module config

### Key Technical Decisions Made
- **Config injection:** Resolved `dict[str, Any]` per module, stored in registry, accessed via `get_module_config(name)`. Same pattern as channel/workflow/runner registries.
- **Bootstrap:** Keep `bootstrap()` function with optional `db_conn` param. No Application class.
- **Transition strategy:** Dual system. CronConfig is a thin bridge (`from_resolved()`), all CLI commands route through new config system. Full CronConfig removal deferred to avoid breaking existing deployments.
- **Dependency naming:** Magento convention — `sequence` for dependencies (like `<sequence>`), `order` for sort position.

---

## Phase 3: Event-Observer System — DONE

### Business Need

Modules currently cannot react to system events without direct coupling. If a notification module wants to alert on job failures, it must import and modify the consumer. If a metrics module wants to track job durations, same problem. Cross-module communication requires editing core code.

For open-source: the event system transforms a "codebase with plugins" into a "platform with an ecosystem." It allows modules to compose without knowing about each other. Example: a `slack-notifications` module observes `job_failed` and posts to Slack — without the Jira module or consumer knowing Slack exists.

### Done

1. **EventManager** (`event_manager.py`) — Magento-style event-observer pattern:
   - `EventManager.dispatch(event_name, event)` — instantiates observers, calls `execute(event)`, swallows errors
   - `EventManager.register(event_name, ObserverEntry)` — registers an observer with name, class, and order
   - Observer classes implement `execute(event)` method (Magento's `ObserverInterface`)
   - Deterministic execution order by `(order, name)` — lower order = earlier execution
   - Module-level registry with `get_event_manager()` / `clear()` — matches channel/workflow/runner pattern

2. **Event data classes** (`events.py`) — mutable dataclasses (observers can modify data):
   - Job lifecycle: `job_published`, `job_claimed`, `job_succeeded`, `job_failed`, `job_retrying`, `job_dead`
   - Consumer lifecycle: `consumer_started`, `consumer_stopping`
   - Module lifecycle: `module_register`, `module_loaded`, `module_ready`, `module_shutdown`

3. **Observer declarations in `events.json`** (Magento's `events.xml` equivalent):
   - Separate from `di.json` and `system.json` — each Magento concept has its own JSON file
   - `ModuleManifest` gains `observers` field, `scan_modules()` reads `events.json`
   - Bootstrap loads observers before dispatching events, wires them per dependency order

4. **Consumer dispatches events** at all job state transitions:
   - `_try_dequeue()` → `job_claimed`
   - `_finalize_job()` → `job_succeeded` / `job_failed` + `job_retrying` or `job_dead`
   - `run()` → `consumer_started` / `consumer_stopping`
   - Zero-observer dispatch is a no-op — zero existing test behavior changed

5. **Publisher dispatches `job_published`** after successful job insertion.

6. **Bootstrap lifecycle events** dispatched in dependency order:
   - `module_register` (before capabilities), `module_loaded` (after capabilities), `module_ready` (all done)
   - `module_shutdown` dispatched in reverse dependency order during graceful shutdown

7. **Example module** in `app/code/_example/` with `events.json` and observer classes.

8. **All 458 tests pass** throughout.

### Acceptance Criteria

- [x] A module can observe `job_failed` via `events.json` and execute custom logic — zero changes to consumer
- [x] EventManager is testable in isolation — instantiated per test, no global state
- [x] Events are synchronous, deterministic execution order (by `order` field, then name)
- [x] Module lifecycle events dispatched in dependency order (Phase 2 ordering)
- [x] Adding events to consumer changes zero existing test behavior
- [x] At least one example module in `app/code/_example/` demonstrates event observation

### Key Technical Decisions Made
- **Event payload:** Mutable dataclasses per event type — observers can modify data, execution order controlled by `order` field
- **Error in observer:** Swallow and log (Magento approach) — a failing observer never crashes job processing
- **Observer declaration:** `events.json` (Magento's `events.xml`) — separate from `di.json`, keyed by event name
- **Naming:** Magento conventions throughout — `dispatch()`, observer classes with `execute()`, `events.json`

### Decoupling Opportunities Identified (Future Work)
- `cli.py` directly imports `JiraChannel` and `jira_publisher` — framework shouldn't know about Jira
- `jira_publisher.py` backward-compat wrapper creates singleton at import time — should be lazy via events
- `sync.py` hardcodes Jira-specific config — could dispatch `ScheduleSyncEvent` for multi-channel support

---

## Phase 4: Areas / Selective Module Loading — NOT ON ACTIVE ROADMAP

> **Status:** Explicitly parked. There is no current business justification for Areas / selective module loading. Revisit only if module count, deployment topology, or security pressure proves a real need.

### Decision

Areas are not part of the active roadmap for now:
- no selective module loading work is planned
- no runtime-area model is required to unlock the next product milestones
- security boundaries continue to be enforced by process/container separation and secret-handling rules, not by Area declarations

### Revisit Trigger

Bring this back only when at least one of these becomes true:
- module count creates measurable startup/runtime overhead
- deployments need materially different module subsets
- selective loading solves a proven security or operational problem better than existing boundaries

---

## Phase 5: Developer Experience & Open-Source Polish — IN PROGRESS

### Business Need

Technical architecture without developer experience is useless for open-source. A contributor needs to go from "I want to add a Slack integration" to a working module in under 30 minutes. Without generators, documentation, examples, and guardrails, the module system will be used only by the core team.

### Done so far

1. **Unified Python CLI** — `bin/agento` bash wrapper (821 lines) replaced by Python CLI subpackage (`src/agento/framework/cli/`). All commands now available via `agento` console_script (`uv tool install agento`). `bin/agento` kept as thin `exec uv run agento "$@"` wrapper for backward compat.

2. **New standalone commands:**
   - `agento doctor` — checks Python, uv, Docker, Compose, Node.js, npm, MySQL connectivity
   - `agento init <project>` — scaffolds project with Docker Compose config
   - `agento up` / `agento down` / `agento logs` — Docker Compose lifecycle wrappers
3. **Python packaging improvements:** optional deps (`agento[dev]`, `agento[test]`), non-Python file inclusion in wheel (JS, JSON, SQL, templates).

5. **Module generator** (`agento make:module`) and **module validation** (`agento module:validate`) — DONE (Phase 5 items from earlier work).

### Remaining Scope

1. **Extension API documentation** — one guide per capability:
   - How to add a channel (Channel protocol, prompt fragments, work discovery)
   - How to add a workflow (Workflow ABC, job argument extraction)
   - How to add a tool (MCP tool registration, config fields)
   - How to add a runtime (Runner protocol, token management)
   - Config system: declaring fields, fallback behavior, encryption

2. **Golden path examples:**
   - `app/code/_example/` — complete example module with channel, workflow, tool, and event subscriber

3. **Architecture tests (boundary enforcement):**
   - Modules cannot import `agento.framework` internals — only `agento.framework.contracts`
   - Modules cannot import other modules directly — only through framework registries
   - Framework does not import modules
   - Automated CI check

4. **Contributing guide** — module development workflow, testing standards, PR checklist, contract versioning policy

### Acceptance Criteria

- [x] `agento make:module slack` produces a skeleton that passes `module:validate`
- [x] `agento doctor` / `agento init` / `agento up` provide a golden path for new users
- [ ] Documentation covers all capability types with copy-paste examples
- [ ] Architecture tests run in CI and fail on boundary violations
- [ ] A developer unfamiliar with the codebase can create a working tool module in <30 minutes

---

## Phase 6: Core Module Refactoring — DONE

### Business Need

Phases 1–3 built powerful framework features (module registries, scoped config, event-observer system) but core modules still used pre-framework patterns: direct imports between framework and Jira, singleton wrappers, hardcoded source strings. The framework was ready — the core modules needed to catch up.

### Done (Phase 3→6 bridge work)

1. **All business logic moved from framework to core modules:**
   - Jira module: channel, workflows (todo/cron/followup), task_list, toolbox_client, models, mention_detector, sync, crontab
   - Claude module: TokenClaudeRunner, output parser (parse_claude_output)
   - Codex module: TokenCodexRunner
   - `framework/jira_publisher.py` deleted — publishing functions live in jira module

2. **CLI command registry** — new framework feature (`framework/commands.py`):
   - `Command` protocol in `framework/contracts/`
   - Modules contribute CLI commands via `di.json` `commands` key
   - Bootstrap loads commands; `cli.py` dispatches from registry
   - Jira module contributes: sync, exec:cron, exec:todo, publish
   - Framework `cli.py` has **zero imports from any module**

3. **Architecture boundary enforced:** `grep -r "from agento.modules" src/agento/framework/` returns nothing.

4. **Toolbox JS co-located with modules** — one module = complete package (Python + JS):
   - Toolbox framework moved from `docker/toolbox/` to `src/agento/toolbox/` (server, config-loader, adapters)
   - `core` module created at `src/agento/modules/core/` with generic tools (email, schedule, browser)
   - Jira MCP tools and REST API routes moved to `src/agento/modules/jira/toolbox/`
   - Convention-based discovery: any `.js` in `<module>/toolbox/` auto-discovered, must export `register(server, context)`
   - Context injection (`{ log, db, playwright, app }`) replaces global imports — testable, decoupled
   - User modules in `app/code/` can now ship custom JS toolbox tools

### Completed (remaining work)

5. **CronConfig eliminated** — `framework/config.py` deleted. Consumer, CLI, e2e all use `DatabaseConfig + ConsumerConfig + AgentManagerConfig` directly. Test fixtures updated.

6. **Auth strategy pattern** — `AuthStrategy` protocol in `agent_manager/auth.py`. Each runtime module (claude, codex) contributes its own auth flow via `di.json` `auth_strategies` key. Zero mixing.

7. **Replay command delegation** — `replay.py` uses `AgentProvider` enum + runner registry for command building. No hardcoded `if agent_type == "claude"` dispatch.

8. **cron.json eliminated** — `_load_framework_config()` reads env vars only. Jira deployment config moved to `.cron.env` using `CONFIG__JIRA__*` format. `docker/cron/cron.json` deleted.

9. **Toolbox env vars unified** — All `.toolbox.env` entries (`SQL_TIMEOUT_SECONDS`, `PLAYWRIGHT_TOOL_WHITELIST` (since retired in favour of per-tool `is_enabled` keys), `JIRA_CREATE_ISSUE_LIMIT_PER_HOUR`, etc.) migrated to `CONFIG__` format. Toolbox JS tools read from `moduleConfigs` (resolved via 3-level fallback) instead of raw `process.env`. New `resolveModuleField()` in config-loader.js mirrors Python's `config_resolver.resolve_field()`.

#### Future Refactoring Areas (Identify as Framework Features Grow)

- **Phase 4 (Areas):** After selective module loading, audit which modules load unnecessary capabilities in each area
- **Phase 5 (DX):** After `module:validate`, run it against core modules to verify they follow the same rules as user modules
- Architecture boundary tests (Phase 5) will systematically catch remaining coupling

### Acceptance Criteria

- [x] `cli.py` has zero imports from `agento.modules.*`
- [x] `framework/jira_publisher.py` is deleted — Jira publishing lives entirely in Jira module
- [x] All business logic (channels, workflows, runners, sync, crontab, task_list, models) lives in core modules
- [x] Adding a second channel requires zero changes to `cli.py`
- [x] CLI command registry enables module-contributed commands via `di.json`
- [x] All existing tests pass throughout (458 unit tests)
- [x] `CronConfig` eliminated — consumer/CLI use decomposed configs
- [x] `auth.py` uses auth strategy pattern — each runtime contributes its auth flow
- [x] `replay.py` delegates command building via runner registry
- [x] `cron.json` eliminated — framework config reads env vars only
- [x] Toolbox JS tools read from `moduleConfigs` (3-level fallback) instead of raw `process.env`
- [x] All 477 Python + 31 JS tests pass

---

## Phase 7: Module Setup System — DONE

### Business Need

The framework's migration system was flat — only `framework/sql/*.sql` files tracked in `schema_migration`. Modules could not contribute their own schema or data migrations. Cron jobs were hardcoded in `entrypoint.sh` — adding a module with scheduled work meant editing Docker infrastructure files.

For open-source: a module author needs to declare "I need these tables" and "I need these cron jobs" in their module directory, run `setup:upgrade`, and be done. No framework files touched.

### Done

1. **Module schema migrations** — modules contribute SQL in `<module>/sql/*.sql`. `schema_migration` table has `module` column. Framework = `'framework'`, modules = `'<name>'`. Applied by `setup:upgrade` in dependency order.

2. **Data patches** (`data_patch.json`) — companion file declares Python patch classes implementing `DataPatch` protocol (`apply(conn)` + `require()` for ordering). Topological sort by `require()` across all modules. Tracked in `data_patch` table.

3. **Cron job declarations** (`cron.json`) — companion file declares scheduled CLI commands. Framework wraps with env loading and Docker paths. `AGENTO:BEGIN/END` markers separate from Jira's `JIRA-SYNC:BEGIN/END` dynamic block. Framework-level `cron.json` handles system jobs (logrotate).

4. **`setup:upgrade` CLI command** — single entry point for all system updates after module file changes:
   - Framework SQL migrations
   - Module SQL migrations (dependency order)
   - Data patches (topological order by `require()`)
   - Cron installation
   - `--dry-run` shows all pending work grouped by type

5. **Entrypoint cleanup** — hardcoded crontab entries and automatic migration removed from `entrypoint.sh`. Entrypoint calls `setup:upgrade` on container start.

6. **Old `migrate` command removed** — `setup:upgrade` is the only CLI interface. Internal `migrate()` function kept for `setup.py` use.

7. **All 527+ tests pass** throughout.

### New Framework Files

| File | Purpose |
|------|---------|
| `framework/setup.py` | `setup:upgrade` orchestrator |
| `framework/data_patch.py` | DataPatch protocol + executor (topological sort, tracking) |
| `framework/crontab.py` | CrontabManager — collect, diff, apply crontab from module declarations |
| `framework/cron.json` | System-level cron jobs (logrotate) |
| `framework/sql/011_module_migrations.sql` | Add `module` column to `schema_migration` |
| `framework/sql/012_data_patches_table.sql` | Create `data_patch` tracking table |

### Module Companion Files Added

| File | Pattern |
|------|---------|
| `<module>/sql/*.sql` | Schema migrations (like `framework/sql/`) |
| `<module>/data_patch.json` | Data patch declarations |
| `<module>/cron.json` | Cron job declarations |

### Acceptance Criteria

- [x] `setup:upgrade` applies framework migrations, then module migrations in dependency order
- [x] Module data patches declared in `data_patch.json` are applied and tracked in `data_patch` table
- [x] `cron.json` declarations from all modules are collected and installed to system crontab
- [x] `--dry-run` shows all pending work grouped by type (framework/module migrations, patches, cron changes)
- [x] `entrypoint.sh` calls `setup:upgrade` on container start
- [x] Jira module's `JIRA-SYNC:BEGIN/END` dynamic cron block is untouched by framework's `AGENTO:BEGIN/END` block
- [x] Existing `schema_migration` entries preserved with `module='framework'`
- [x] All existing tests pass throughout

### Key Technical Decisions Made

- **`setup:upgrade` is the single entry point** for all system updates. No standalone `migrate` or `cron:install` commands. One command, one mental model.
- **`data_patch.json` for data, `sql/` for schema** — data patches are named classes with `require()` ordering (Magento's `DataPatchInterface`). SQL migrations are numbered files (sequential, position-dependent). Different semantics, different mechanisms.
- **`db_schema.json` reserved for future declarative schema** — not part of Phase 7. See post-MVP roadmap below.
- **DataPatch protocol: `apply(conn)` + `require()`** — `require()` returns fully-qualified `module/PatchName` strings. Topological sort across all modules using Kahn's algorithm. Cycle detection raises error.
- **Singular table names** (migration 013) — `schema_migration`, `data_patch`, `job`, `schedule`, `oauth_token`.
- **Two marker blocks in crontab** — `AGENTO:BEGIN/END` for static module jobs, `JIRA-SYNC:BEGIN/END` for dynamic per-issue jobs. Clean separation.
- **Command wrapping** — `cron.json` declares CLI subcommands (e.g. `"sync"`), framework wraps with env loading and paths. Modules don't know about Docker paths.

---


## Phase 8: Event Coverage & Naming Convention — PARTIAL

### Business Need

The event-observer system exists, but many framework/core extension points still rely on direct calls, implicit behavior, or missing events. For open-source, explicit events are the safest extensibility mechanism: discoverable, grep-friendly, reviewable, and stable across modules.

### Done

1. **Event naming convention established:**
   - Framework events (new): `agento_<area>_<action>` (e.g., `agento_config_saved`)
   - Third-party module events: `<vendor>_<module>_<event>` (e.g., `acme_slack_message_sent`)
   - Existing events keep their names for backward compatibility

2. **Config & setup events** (Phase 8 initial):
   - `agento_config_saved` — after CLI `config:set`
   - `agento_setup_before` — before `setup:upgrade` begins
   - `agento_setup_complete` — after `setup:upgrade` finishes
   - `agento_migration_applied` — after each SQL migration
   - `agento_data_patch_applied` — after each data patch
   - `agento_crontab_installed` — after crontab updated

3. **Worker & agent_view lifecycle events** (added in Phase 9.5):
   - `agento_worker_started` — worker slot begins job execution
   - `agento_worker_stopped` — worker slot finishes
   - `agento_agent_view_run_started` — agent_view runtime initialized
   - `agento_agent_view_run_finished` — agent_view run completed

4. **Routing events** (added in Phase 10):
   - `agento_routing_resolved` — successful routing to agent_view
   - `agento_routing_ambiguous` — multiple routers matched
   - `agento_routing_failed` — no router matched

5. **25 total event classes** covering job lifecycle, consumer lifecycle, worker lifecycle, agent_view runs, module lifecycle, configuration, setup, migrations, and routing.

6. **Code review checklist updated** — event naming and extensibility checks in `agento-code-review` skill

7. **Documentation refreshed** — naming convention, new events table, "When to Add an Event" guidance in `docs/architecture/events.md`

### Remaining (incremental, alongside Phases 11–13)

Events for features that don't exist yet — add when each phase introduces the feature:

- tool binding change events (when dynamic tool binding is added)

### Acceptance Criteria

- [x] New framework/core extension points dispatch explicit events where module extensibility is expected
- [x] Event names follow `agento_<area>_<action>` consistently
- [x] `code-review/skill.md` includes event naming / extensibility checks
- [x] Documentation explains when to add an event and when not to
- [x] Workspace/agent_view lifecycle events (Phase 9.5)
- [x] Routing events (Phase 10)

### Key Technical Decisions

- **Prefer domain/lifecycle events, not interception:** no generic ORM-style `before_save/after_load` magic
- **Stable names over clever names:** event ownership must be obvious from the string itself
- **Events stay synchronous:** keep debugging and ordering simple
- **Config events from CLI only:** `agento_config_saved` fires from `config:set` CLI, not internal bootstrap — prevents noisy events during startup

---

### Post-MVP: Declarative Schema (`db_schema.json`)

> **Status:** Not on active roadmap. Planned for after the module system stabilizes and real-world usage reveals whether imperative SQL migrations (`sql/`) are sufficient or declarative convergence is needed.

**Concept:** Module declares desired table structure in `db_schema.json` (Magento's `db_schema.xml` equivalent). `setup:upgrade` compares declared vs actual schema and generates DDL actions.

**How it would work:**
1. Module declares tables/columns/indexes in `db_schema.json` (declarative JSON format)
2. `setup:upgrade` fetches current DB schema via `INFORMATION_SCHEMA`
3. Compares declared schema against actual schema
4. Generates DDL actions (CREATE TABLE, ALTER TABLE ADD/MODIFY/DROP COLUMN, ADD/DROP INDEX)
5. Applies actions (with `--dry-run` preview)

**Key design:** Declarative, not imperative — module says "I need this table with these columns", framework figures out what to do. Complements `sql/` migrations (imperative) for cases where convergent schema is preferred over ordered migrations.

**Revisit trigger:** When module count grows large enough that hand-writing sequential SQL migrations becomes a maintenance burden, or when third-party modules need schema portability across database versions.

---

## Phase 9: Workspace & Agent View Hierarchy — DONE

### Business Need

Agento is evolving toward a Magento-like, single-organization deployment model where one installation hosts multiple workspaces and multiple agent variants. To make this usable, the framework needs first-class scopes for configuration and composition — without cloning modules or hardcoding per-agent behavior.

### Scope

1. **Introduce scope hierarchy:**
   - `global`
   - `workspace`
   - `agent_view`

2. **Add first-class entities:**
   - `workspace`
   - `agent_view`

3. **Config inheritance only (MVP):**
   - fallback from `agent_view` → `workspace` → `global`
   - inheritance applies to configuration paths and selected bindings only
   - no generic inheritance of arbitrary database columns across all entities

4. **Shared source of truth for Python + Node.js runtimes:**
   - both Python services and the Node.js toolbox read the same DB-backed scoped configuration
   - do **not** migrate toolbox to Python just to share config logic
   - schema + config path conventions are the shared contract, not a cross-language ORM

5. **Prepare composition model:**
   - `agent_view` becomes the main unit of configuration and runtime identity
   - later features (routing, tool binding, locale policy) build on this hierarchy

6. **Agent CLI config population from 4-step fallback:**
   - Map major Claude Code and Codex CLI config entries (model, personality, MCP servers, trust level, etc.) to agento config fields in `system.json` / `config.json`
   - Before each worker run, generate agent CLI config files (`.codex/config.toml`, `.mcp.json`, `.claude.json`, `.claude/settings.json`) from resolved scoped config
   - Each `agent_view` can override model, MCP server URL, personality, tool bindings — resolved via `agent_view` → `workspace` → `global` fallback
   - Eliminates hand-edited config files in `workspace/` — single source of truth in agento's config system

### Done

1. **DB schema** (`sql/014_workspace_agent_view.sql`) — `workspace` and `agent_view` tables, `core_config_data.scope` widened to VARCHAR(16), `job.agent_view_id` column with FK constraint

2. **Models** (`workspace.py`) — `Workspace` and `AgentView` dataclasses with `from_row()`, DB query functions (`get_active_agent_views`, `get_workspace`, `get_agent_view`, `get_agent_view_by_code`)

3. **Scoped config resolution** (`scoped_config.py`) — 3-tier DB fallback (`agent_view` → `workspace` → `global`) integrated into 3-level chain (ENV → scoped DB → config.json). Reuses `_resolve_from_db` and type coercion from `config_resolver.py`.

4. **Agent config writer** (`agent_config_writer.py`) — generates `.claude.json`, `.claude/settings.json`, `.mcp.json`, `.codex/config.toml` from resolved scoped config paths (`agent/*`)

5. **Agent view worker** (`agent_view_worker.py`) — subprocess launcher per `agent_view` with isolated working directories, env var injection, graceful shutdown (SIGTERM → SIGKILL)

6. **Tests** — 67 tests across 4 test files covering models, scoped resolution, config generation, and worker lifecycle

### Acceptance Criteria

- [x] `workspace` and `agent_view` exist as first-class core entities
- [x] Config resolution supports `global` → `workspace` → `agent_view` fallback
- [x] An `agent_view` can override only selected config values and inherit the rest
- [x] Python and Node.js resolve the same effective value for the same scoped config path
- [x] No generic EAV-style inheritance is introduced for arbitrary entity fields
- [x] Agent CLI config files are generated from resolved scoped config before each worker run

### Key Technical Decisions

- **Single-org model:** no `company` / `organization` scope for now
- **Config-first inheritance:** start with scoped config, not full entity inheritance
- **Language split stays:** Node.js toolbox and Python execution remain separate by design
- **Generated config files:** agent CLI tools read their native config format (TOML, JSON) — agento generates these from its own config system rather than teaching CLIs to read agento config directly

---

## Phase 9.5: Concurrent Agent View Execution Pool — DONE

### Business Need

Phase 9 introduced `workspace`, `agent_view`, scoped configuration, generated agent CLI config files, and the `agent_view_worker` launcher. MVP still needs the missing runtime layer: a bounded execution pool that consumes jobs in parallel, runs different `agent_view` profiles at the same time, and isolates filesystem state per run without introducing a new platform or credential service.

The practical target is simple: one consumer process, configurable worker limit (for example `5`), explicit `agent_view` routing, concurrent execution for profiles like `developer`, `team-leader`, and `qa-tester`, and predictable prioritization so urgent work is not blocked by bulk QA.

### Scope

1. **Fixed-size worker pool in the consumer**
   - Add a configurable pool (`consumer/max_workers`) to the existing consumer.
   - Worker slots are generic. They are not permanently bound to one `agent_view`.
   - MVP target: single host, single consumer process, fixed parallelism.

2. **`agent_view` as the runtime profile**
   - Each job must carry `agent_view_id`.
   - Effective runtime comes from the resolved `agent_view` profile: provider, model, generated Claude/Codex config, MCP config, and instruction files.
   - Example supported in MVP:
     - `developer` → provider `codex`, model `gpt-5.4`
     - `team-leader` → provider `claude`, model `opus-4.6`
     - `qa-tester` → provider `claude`, model `sonnet-4.6`

3. **Per-run isolated working directory**
   - Every claimed job gets its own run directory.
   - Generated `.claude.json`, `.claude/settings.json`, `.mcp.json`, and `.codex/config.toml` are written into that run directory before execution.
   - `AGENTS.md` and `SOUL.md` are resolved per `agent_view` with workspace-level fallback.
   - Workers do not keep a permanent directory; directories are allocated per run and cleaned up after completion.

4. **Simple job scheduling**
   - Keep the current MySQL queue model.
   - Claim jobs in priority order, then FIFO inside the same priority.
   - Add `job.priority` and fill it from effective `agent_view` scoped config.
   - Priority range is `0-100`, where higher means earlier execution.

5. **Priority is configured per `agent_view`**
   - Introduce scoped config path `agent_view/scheduling/priority`.
   - Resolve it with existing fallback: `agent_view` → `workspace` → `global`.
   - When a job is published, copy the resolved value into `job.priority`.
   - Changing priority affects newly published jobs; existing queued jobs keep their stamped priority.

6. **Concurrent runs of the same `agent_view` are allowed**
   - MVP explicitly allows multiple `RUNNING` jobs for the same `agent_view`.
   - No `max 1 RUNNING per agent_view` guard is added in Phase 9.5.
   - Current OAuth / CLI auth collision risk is accepted for MVP.

7. **Health, timeouts, and shutdown**
   - Each worker slot runs one job at a time and supervises one runtime subprocess.
   - If a subprocess exits unexpectedly, only the current job is failed or retried; the consumer keeps running.
   - On SIGTERM, the consumer stops claiming new jobs, waits for in-flight jobs up to the existing timeout / grace window, then terminates remaining subprocesses.

8. **Minimal observability and events**
   - Add structured logging fields for `job_id`, `agent_view_id`, `worker_slot`, `provider`, `model`, `priority`, and `run_dir`.
   - Dispatch runtime lifecycle events:
     - `agento_worker_started`
     - `agento_worker_stopped`
     - `agento_agent_view_run_started`
     - `agento_agent_view_run_finished`

### Acceptance Criteria

- [x] Consumer can execute up to `N` jobs concurrently with configurable limit (example: `5`)
- [x] Different `agent_view` profiles can run at the same time with different provider/model/configuration
- [x] The same `agent_view` can have more than one concurrent `RUNNING` job in MVP
- [x] Each job gets an isolated run directory and freshly generated agent CLI config files
- [x] `AGENTS.md` and `SOUL.md` can differ per `agent_view`
- [x] Job claiming honors `priority DESC, created_at ASC`
- [x] `job.priority` is derived from scoped `agent_view` configuration in the `0-100` range
- [x] Worker failure does not crash the whole consumer; only the affected job is failed or retried
- [x] Consumer performs graceful shutdown of the whole pool on SIGTERM
- [x] Runtime lifecycle events for workers / agent_view runs are dispatched with `agento_<area>_<action>` naming

### Key Technical Decisions

- **Keep the current queue architecture:** MySQL `SELECT ... FOR UPDATE SKIP LOCKED`, no Redis/Celery/Kafka in MVP.
- **Prefer worker slots over dedicated agent daemons:** a worker slot is a pool resource, not a long-lived identity.
- **Allocate filesystem state per run, not per worker:** simpler cleanup, fewer stale-state bugs, no shared-write collisions in work directories.
- **Keep explicit routing:** `job.agent_view_id` is required; no semantic or rule-based routing in Phase 9.5.
- **Keep toolbox boundary intact:** Phase 9.5 does not introduce a new credential service; heavier secret brokering stays in Phase 12.
- **Accept OAuth refresh-token risk in MVP:** maximizing parallelism is more important than solving shared-refresh correctness right now.
- **Stay single-host in MVP:** horizontal scaling is deferred until this pool model proves itself.

### Post-MVP

- Solve shared OAuth refresh-token collisions by introducing a token-sink / profile-routing approach inspired by OpenClaw.
- Add optional per-provider auth isolation so concurrent runs no longer depend on the same mutable auth files.
- If required later, scale out to multiple consumers with lease-based coordination per claimed job or per runtime slot.

---

## Phase 10: Ingress Identities & Agent Resolution — DONE

### Business Need

Agento needs a deterministic and extensible way to map incoming Outlook / Teams / API traffic to the right `agent_view`. Routing must be debuggable and module-extensible, not hidden inside one integration.

### Scope

1. **Inbound identity model (MVP):**
   - bind inbound identities directly to `agent_view`
   - supported identity types: Outlook email/mailbox aliases, Teams identities, API client identities

2. **Router registry:**
   - modules can register resolvers/routers via manifests
   - deterministic ordering, same philosophy as other registries
   - framework knows the contract, modules provide implementations

3. **Default deterministic routing (MVP):**
   - direct identity-to-agent mapping
   - rule-based routing hooks for future module additions
   - ambiguity handling is explicit, never silent

4. **Routing observability:**
   - log matched router, candidates, chosen `agent_view`, and reason
   - dispatch routing-related events for metrics / notifications / debugging

5. **Post-MVP / nice to have:**
   - semantic router based on agent competence/description using LLM

### Done

1. **Ingress identity model** (`ingress_identity.py`) — `IngressIdentity` dataclass with `bind_identity()`, `unbind_identity()`, `get_ingress_identity()`, `list_identities()`. DB schema in `sql/015_ingress_identity.sql`.

2. **Router registry & protocol** (`router_registry.py`, `router.py`) — `Router` protocol with `name` property and `resolve()` method. `RoutingContext` dataclass (channel, workflow_type, identity_type, identity_value, payload). `RoutingDecision` with agent_view resolution, matched_router, and ambiguity detection.

3. **`resolve_agent_view()` function** — runs all registered routers, detects ambiguity, returns `RoutingDecision`. Logs matched router, candidates, chosen agent_view, and reasoning.

4. **CLI commands** — `ingress:bind`, `ingress:list`, `ingress:unbind` contributed by core module via `di.json` commands.

5. **Routing events** — `agento_routing_resolved`, `agento_routing_ambiguous`, `agento_routing_failed` dispatched during resolution.

6. **Jira module integration** — `_resolve_routing()` calls `resolve_agent_view()` during job publishing (cron, todo, followup workflows).

### Acceptance Criteria

- [x] Outlook, Teams, and API ingress can resolve to an `agent_view` without framework code changes
- [x] A new module can contribute a router through the registry
- [x] Ambiguous matches are surfaced explicitly and logged with reasoning
- [x] Default routing works deterministically without LLM involvement
- [x] Semantic routing is documented as post-MVP only

### Key Technical Decisions

- **MVP starts simple:** direct binding from inbound identity to `agent_view`
- **Extensibility via registry:** do not hardcode routing logic into one channel
- **LLM routing later:** competence matching is valuable, but not required for the first usable version

---

## Phase 10.5a: Composable Workspace, Skills & Tools — PLANNED

### Business Need

Consumer currently does too much per job: claim, resolve runtime, generate workspace from scratch (configs + instructions), run agent, finalize. Each run regenerates identical files. Agent_views cannot compose skills or filter tools without manual DB edits. The framework needs composable, pre-built workspaces per agent_view — and CLI-managed skill/tool control.

### Scope

Three independent deliverables (3 PRs, can be developed in parallel):

1. **composable_tools** — CLI commands (`tool:list`, `tool:enable`, `tool:disable`) wrapping the existing `isToolEnabled()` mechanism in toolbox. Uses `core_config_data` with path `tools/{name}/is_enabled` and scoped overrides. Zero new tables.

2. **composable_skills** — New `skill` module. Skills scanned from disk (`app/skills/{name}/SKILL.md`), registry in DB (`skill_registry` table). Enable/disable per agent_view via `core_config_data` with path `skill/{name}/is_enabled`. CLI: `skill:sync`, `skill:list`, `skill:enable`, `skill:disable`.

3. **composable_workspace** — New `workspace_build` module. Materialized build per agent_view at `/workspace/{ws}/{av}/builds/{id}/` containing AGENTS.md, SOUL.md, CLAUDE.md, .claude.json, .mcp.json, .codex/config.toml, and enabled skills. `current` symlink points to active build. Consumer copies from build to run_dir instead of generating from scratch. CLI: `workspace:build`, `workspace:build-status`.

### Module Dependency Design

All three use exclusively framework code — no inter-module imports:
- `composable_tools`: commands in `agent_view` module (sequence: `["core"]`), uses framework's `scoped_config_set()`, `scan_modules()`
- `composable_skills`: new module (sequence: `["core"]`), uses framework's `scoped_config_set()`, `build_scoped_overrides()`
- `composable_workspace`: new module (sequence: `["core"]`), uses framework's `populate_agent_configs()`, `build_scoped_overrides()`. Instruction writing is inline (not imported from `agent_view`). Skill integration via `try/except ImportError` (soft dependency, no `sequence` entry).

Each module can be disabled independently without breaking the system.

### Acceptance Criteria

- [ ] `tool:list --agent-view developer` shows effective enabled/disabled state per tool
- [ ] `tool:enable`/`tool:disable` set scoped config that toolbox respects on next MCP session
- [ ] `skill:sync` scans disk and upserts to `skill_registry`
- [ ] `skill:enable`/`skill:disable` control per-agent_view skill inclusion
- [ ] `workspace:build --agent-view developer` materializes a complete build directory
- [ ] Consumer uses pre-built workspace when available, falls back to on-the-fly generation
- [ ] Disabling any of the 3 modules leaves the system fully operational
- [ ] All existing tests pass throughout

### Key Technical Decisions

- **Reuse `core_config_data` for enable/disable** — both tools and skills use the same scoped config pattern (`{type}/{name}/is_enabled`). No new assignment tables.
- **Builds are triggered manually (CLI)** — no auto-rebuild on config change in this phase.
- **`current` symlink model** — simple, atomic, no registry lookup needed at runtime.
- **Inline instruction writing in builder** — 15 lines duplicated from `agent_view` module to avoid hard inter-module dependency. Preferable over coupling.
- **Soft skill dependency** — `try/except ImportError` pattern keeps workspace_build independent of skill module.

---

## Phase 10.5b: Composable Workspace Automation — PLANNED

### Business Need

Phase 10.5a introduces manual `workspace:build` and `skill:sync`. For production use, builds should auto-trigger on config changes, old builds should be cleaned up, and skill sync should run periodically.

### Scope

1. **Dirty flag on workspace_build** — when something changes that affects a built workspace, mark the build as dirty (DB column `is_dirty` on `workspace_build` or a separate `workspace_build_status` per agent_view). Consumer checks dirty flag before job start and triggers rebuild if needed.

   **Triggers that mark workspace as dirty:**
   - `config:set` on paths that affect workspace: `agent/*`, `agent_view/*`, `skill/*`, `tools/*` (observer on `agento_config_saved`)
   - `module:enable` / `module:disable` (observer on `module_status_changed`)
   - `skill:enable` / `skill:disable` (observer on `agento_config_saved` with `skill/` prefix)
   - `skill:sync` when new/updated skills detected (observer on `skill_sync_complete_after`)
   - agent_view created / deleted / reassigned to different workspace
   - Theme files changed on disk (detected by cron job, stat-based)

   **Open question:** Mark all agent_views dirty, or only affected ones? For scoped config changes the scope tells us which agent_view(s) are affected. For module enable/disable and theme changes — all agent_views are dirty.

2. **Observer on `agento_config_saved`** — auto-rebuild affected agent_views when workspace-impacting config paths change. Observer registered in `workspace_build/events.json`. Sets dirty flag rather than rebuilding synchronously (rebuild can be slow, should not block config:set).

3. **Consumer pre-flight rebuild** — before creating runtime dir, consumer checks if current build is dirty. If yes, triggers `execute_build()` inline. This ensures agents always run on fresh config without requiring external cron.

4. **GC of old builds** — keep last N builds per agent_view (configurable via `workspace_build/keep_builds`, default 5). CLI: `workspace:gc [--agent-view <code>] [--dry-run]`. Can also run as observer after successful build.

5. **GC of old runtime dirs** — runtime dirs are no longer cleaned up after job completion (they hold agent artifacts for review). Periodic cleanup of runtime dirs older than N days (configurable). CLI: `workspace:gc-runtime [--days 7] [--dry-run]`.

6. **Cron job: periodic `skill:sync` + rebuild** — declared in `skill/cron.json` and `workspace_build/cron.json`. Runs `skill:sync` periodically, then `workspace:build --all` to pick up any skill content changes.

### Acceptance Criteria

- [ ] Changing a scoped config value via `config:set` marks affected agent_view builds as dirty
- [ ] `module:enable`/`module:disable` marks all builds as dirty
- [ ] `skill:enable`/`skill:disable` marks affected agent_view builds as dirty
- [ ] Consumer auto-rebuilds dirty workspaces before job execution
- [ ] Old builds are automatically cleaned up, keeping last N
- [ ] Old runtime dirs are cleaned up after configurable retention period
- [ ] `skill:sync` and `workspace:build --all` run on a configurable cron schedule
- [ ] All existing tests pass throughout

---

## Phase 11: Admin API & Agent Studio (MVP)

### Business Need

If creating a new workspace or agent requires hand-editing JSON, SQL rows, or deployment files, the framework will stay developer-only. Agento needs a minimal but real control plane for operators to create and manage `workspace` / `agent_view` configurations safely.

### Scope

1. **Admin API service:**
   - backend API for the admin frontend
   - backend API for external integrations and operational automation
   - uses the same core contracts and DB source of truth as CLI/framework code

2. **Agent Studio MVP capabilities:**
   - manage `workspace`
   - manage `agent_view`
   - manage scoped config overrides
   - attach tools from toolbox
   - manage allowlists / whitelists

3. **Binding-first model:**
   - configure references and bindings, not ad hoc free-form blobs
   - keep agent runtime configuration explicit and inspectable

4. **Admin frontend stays separate:**
   - UI/frontend is not a framework runtime area
   - the framework exposes APIs and core behavior; the admin app consumes them

### Acceptance Criteria

- [ ] An operator can create a `workspace` from the admin flow
- [ ] An operator can create an `agent_view` and configure scoped overrides
- [ ] Tools can be attached from toolbox through explicit bindings
- [ ] Allowlists / whitelists can be managed without manual DB edits
- [ ] API, CLI, and runtime resolve the same effective configuration

### Key Technical Decisions

- **API first:** the admin frontend is a client of the API, not a special-case runtime
- **MVP stays narrow:** no generic CRUD generator for every table
- **Bindings over magic:** core relationships should stay explicit

---

## Phase 12: Credential Broker / Key Vault

### Business Need

The framework needs a safer and cleaner way to manage secrets than passing credentials around in config or containers. Admin needs a write path for credentials; toolbox needs a read path; agent execution must stay isolated from secret storage.

### Scope

1. **Introduce a dedicated credential broker service** in a separate container:
   - stores secret material securely
   - authenticates clients with scoped broker tokens
   - returns secret values only to authorized runtimes

2. **Access model:**
   - Admin API can create/update/revoke secrets
   - Toolbox can read secrets needed to execute tools
   - Python execution runtimes (`consumer` / cron) never get direct vault access by default

3. **MVP implementation prioritizes simplicity:**
   - start with an Agento-owned broker service and a minimal API
   - store encrypted secret material in Agento-managed storage behind the broker
   - optional Azure Key Vault integration can come later as a module/adapter

4. **Reference-based configuration:**
   - config stores secret references, not raw secret values
   - tool bindings resolve references through the broker

### Acceptance Criteria

- [ ] Admin can create/update/revoke secrets through the broker flow
- [ ] Toolbox can resolve a secret reference at runtime with broker authentication
- [ ] Agent execution runtimes do not receive raw secret storage credentials by default
- [ ] Config and bindings store references/handles, not plain secrets
- [ ] External secret backends (for example Azure Key Vault) are treated as optional integrations, not MVP requirements

### Key Technical Decisions

- **Separate storage from execution:** toolbox may use secrets, but the broker owns secret storage
- **Start simple:** avoid introducing heavyweight secret infrastructure before the product proves the need
- **Broker tokens are scoped:** admin and toolbox should not share identical capabilities

---

## Phase 13: Response Locale Policy

### Business Need

The immediate multilingual need is not translating the whole admin or module ecosystem. It is controlling the language used in responses sent to end users. This needs to be configurable per workspace / agent_view without introducing opaque translation steps.

### Scope

1. **Scoped locale policy:**
   - locale can be configured per `workspace` and overridden per `agent_view`

2. **Response language modes (MVP):**
   - `preserve_input_language`
   - `force_output_locale`

3. **Runtime integration:**
   - locale policy becomes part of effective agent configuration
   - prompt-building / runtime execution respects the selected output policy

4. **Explicitly out of MVP:**
   - automatic pre-translation of incoming content by a weaker model
   - full admin/module UI i18n

5. **Nice to have later:**
   - i18n for modules and admin UI once the product surface justifies it

### Acceptance Criteria

- [ ] A `workspace` can define a default response locale
- [ ] An `agent_view` can override the response locale policy
- [ ] Runtime behavior can force output language without translating the whole input beforehand
- [ ] Admin/module i18n is clearly documented as future nice-to-have, not MVP

### Key Technical Decisions

- **Response policy first:** solve the user-facing output problem before framework-wide i18n
- **No hidden translation step in MVP:** keep debugging and prompt quality predictable
- **Scoped like config:** locale policy follows the same hierarchy as `workspace` / `agent_view`


## Phase 14: OAuth Token Pools

### Business Need

Tokens are currently selected per-provider via `TokenResolver`. For multi-tenant deployments, multiple tokens from the same provider should be grouped into pools with capacity-based rotation and per-agent_view pool assignment.

### Scope

1. **New `oauth_pool` table** — groups tokens by provider
2. **Pool assignment via scoped config** (`agent/pool_id`) replacing direct token selection
3. **TokenResolver gains pool-aware selection** (capacity rotation within assigned pool)
4. **Migrate `token_limit` logic** from `select_best_token` to pool-level
5. **Drop `oauth_token.is_primary`** — replaced by pool-level default

### Key Technical Decisions

- Pools are the unit of assignment, tokens are the unit of rotation
- `TokenResolver` is the single extension point — no consumer changes needed
- Existing `is_primary`, `token_limit`, `model` columns on `oauth_token` stay until this phase

---

## Phase 15: Distribution & Installation Model

### Business Need

Agento currently requires contributors to clone the repo and run `docker compose build` locally. The dev-only `docker-compose.yml` bind-mounts host source code into containers, meaning the "built" image is overridden at runtime. The cron image inherits the full sandbox (Claude Code, Codex, Playwright, Chromium, mssql-tools, gosu, poppler-utils) even though cron only needs a Python runtime. Builds are not fully reproducible — `uv.lock` exists but is not copied into Docker builds, toolbox uses `npm install` instead of `npm ci`, and base images use mutable tags.

For adoption: an operator should run `agento install`, answer a few questions, and get a running system from pre-built versioned images — no local builds, no source checkout, no bind-mounts of framework code. The customer template compose (`src/agento/framework/cli/templates/docker-compose.yml`) already uses `image: ghcr.io/agento-cc/agento-*:__AGENTO_VERSION__` with no source bind-mounts — the infrastructure is there, but the build pipeline and image architecture need to catch up.

### Scope

1. **Decouple cron image from sandbox:**
   - Replace `FROM ${SANDBOX_IMAGE}` in `docker/cron/Dockerfile` with a standalone `FROM python:3.12-slim` (or equivalent minimal base).
   - Cron needs: Python 3.12, uv, cron daemon, procps, logrotate, curl, openssh-client. It does not need: Node.js, Claude Code, Codex CLI, Playwright, Chromium, mssql-tools, gosu.
   - Sandbox image continues to carry the full agent runtime (Node.js, Claude Code, Codex, system packages).
   - Update `docker.yml` CI workflow: remove `needs: build-sandbox` dependency for cron; sandbox and cron can build in parallel.
   - Update `docker/cron/entrypoint.sh`: remove sandbox-specific assumptions (gosu, agent user setup inherited from sandbox base).
   - **Migrate agent credential directories** (`workspace/.claude/`, `workspace/.codex/`, `workspace/.claude.json`) out of the workspace volume into a dedicated named volume (e.g., `agent-home:/home/agent`). These are persistent agent state (credentials, history, cache) — they don't belong in the composable workspace tree (theme/build/runtime). Update sandbox entrypoint to stop symlinking from `/workspace/.claude` and instead mount directly to `~/.claude`.

2. **Stabilize Docker builds:**
   - **Cron Dockerfile:** `COPY uv.lock` alongside `pyproject.toml`, use `uv sync --frozen` instead of `uv pip install -e .` so the lock file is respected.
   - **Toolbox Dockerfile:** `COPY src/agento/toolbox/package-lock.json` before `npm ci --omit=dev` (replace `npm install`).
   - **Pin base images:** use digest-pinned base images (e.g., `python:3.12-slim@sha256:...`, `node:22-slim@sha256:...`) in all Dockerfiles. Document the update process.
   - **CI uses versioned sandbox:** change `docker.yml` build-arg from `SANDBOX_IMAGE=ghcr.io/agento-cc/agento-sandbox:latest` to the release version tag so sandbox tag matches the release being built.

3. **Rename dev compose, keep customer compose as default:**
   - Rename `docker/docker-compose.yml` → `docker/docker-compose.dev.yml` — retains bind-mounts (`../src/agento:/opt/cron-agent/src/agento`, `../src/agento/toolbox:/app`), local `build:` directives, and `image: agento-*:latest` tags for core development.
   - The template at `src/agento/framework/cli/templates/docker-compose.yml` (used by `agento install`) is already the customer compose — uses GHCR images, no source bind-mounts. Verify it has no remaining dev artifacts.
   - `agento up` / `agento down` already resolve the correct compose file via `find_compose_file()`. No CLI changes needed.
   - Update developer documentation to reference `docker-compose.dev.yml` for framework development.

4. **Version pinning in customer compose:**
   - `agento install` stamps `__AGENTO_VERSION__` from `get_package_version()`. Verify this resolves to the installed PyPI version (e.g., `0.8.0`), not `0.0.0` or a dev placeholder.
   - Ensure the `"latest"` fallback in `get_package_version()` is only used in development, not in `uv tool install` scenarios.

5. **Customer compose mounts only data/config/user-modules:**
   - Verify template compose mounts only: `workspace:/workspace`, `app/code:/app/code:ro` (user modules), `logs:/app/logs`, `tokens:/etc/tokens`, `storage/mysql:/var/lib/mysql`.
   - No mount of `src/agento/` or `src/agento/toolbox/` in customer path.

### Acceptance Criteria

- [ ] `agento install && agento up` starts a working system from pre-built GHCR images — no `docker compose build` required
- [ ] Dev workflow uses `docker compose -f docker-compose.dev.yml` with bind-mounts and local builds
- [ ] Cron image does not inherit sandbox — built from `python:3.12-slim` base, under 500MB uncompressed
- [ ] Cron Dockerfile uses `uv.lock` (`uv sync --frozen`); toolbox Dockerfile uses `npm ci`
- [ ] Base images are pinned by digest in all Dockerfiles
- [ ] CI builds sandbox and cron in parallel (no `needs: build-sandbox` for cron)
- [ ] CI tags sandbox image with release version, not `:latest`, when building dependent images
- [ ] Customer compose mounts zero framework source directories
- [ ] Version stamped in customer compose matches the installed PyPI package version

### Key Technical Decisions

- **Dev compose is opt-in, not default:** contributors working on framework code explicitly use `-f docker-compose.dev.yml`. The unmarked compose file (generated by `agento install`) is the customer path. This prevents the common failure mode of customers accidentally running dev compose.
- **Cron decoupled from sandbox at the image level:** the two containers have fundamentally different dependency profiles. Cron is a Python service; sandbox is an agent runtime with Node.js, LLM CLIs, and browser automation. Sharing a base image saves build time but wastes ~2GB of runtime footprint and creates a false coupling in the dependency graph.
- **Lock files are the reproducibility contract:** `uv.lock` and `package-lock.json` already exist in the repo. The only change is ensuring Docker builds actually use them. No new tooling required.
- **No Helm/K8s in this phase:** the compose-based model serves single-host deployments. Kubernetes manifests are a future phase if demand materializes.

---

## Phase Dependencies

```text
Phase 0 (DONE)
    │
    ▼
Phase 1: Contracts & Module-Driven Registries (DONE)
    │
    ▼
Phase 2: Framework Kernel & Scoped Config (DONE)
    │
    ▼
Phase 3: Event-Observer System (DONE)
    │
    ├──────────────────────────┐
    ▼                          ▼
Phase 6: Core Module       Phase 7: Module Setup
Refactoring (DONE)         System (DONE)
                               │
                               ▼
                       Phase 9: Workspace &
                       Agent View Hierarchy (DONE)
                               │
                               ▼
                       Phase 9.5: Concurrent Agent
                       View Execution Pool (DONE)
                               │
                               ▼
                       Phase 10: Ingress Identities
                       & Agent Resolution (DONE)
                               │
                               ▼
                       Phase 10.5a: Composable
                       Workspace, Skills & Tools
                               │
                               ▼
                       Phase 10.5b: Composable
                       Workspace Automation
                               │
                               ▼
                       Phase 11: Admin API &
                       Agent Studio (MVP)
                               │
                               ▼
                       Phase 12: Credential Broker /
                       Key Vault
                               │
                               ▼
                       Phase 13: Response Locale Policy
                               │
                               ▼
                       Phase 14: OAuth Token Pools

Phase 8: Event Coverage & Naming Convention (PARTIAL)
    └─ 25 events total; remaining events added incrementally with Phases 11–13

Phase 5: DX & Open-Source Polish (IN PROGRESS)
    └─ CLI commands done; docs and architecture tests remain

Phase 15: Distribution & Installation Model
    └─ independent of Phases 9–14; depends only on existing CI/CD pipeline
    └─ can execute in parallel with any active phase

Phase 4: Areas / Selective Loading
    └─ parked, not on active roadmap
```

Phases 0–3, 6–7, 9, 9.5, and 10 are done — the framework has a complete module system, scoped configuration hierarchy (`workspace` / `agent_view`), concurrent execution pool with priority scheduling, and ingress identity routing. Phase 8 established the `agento_<area>_<action>` naming convention with 25 events covering job/consumer/worker/routing lifecycle; remaining events will be added as Phases 11–13 introduce new features. The next work is Phase 10.5a (Composable Workspace, Skills & Tools) — three independent PRs adding CLI-managed tool/skill control and pre-built workspaces per agent_view, followed by Phase 10.5b (automation: auto-rebuild, GC, cron sync). Then Phase 11 (Admin API & Agent Studio), credential brokering, response locale policy, and OAuth token pools. Phase 15 (Distribution & Installation Model) is independent and can execute in parallel. Phase 5 follows once contracts are stable enough to document. Phase 4 is explicitly parked.

---

## Anti-Patterns to Avoid

1. **Don't split integrations into typed micro-modules** — one `jira` module, not `channel-jira` + `tool-jira` + `workflow-jira`
2. **Don't copy Magento's XML hell** — `module.json` is the only manifest, keep it simple
3. **Don't build DI container** — Python's import system + constructor injection is enough
4. **Don't add interception/plugin system** — events for cross-cutting concerns; direct code for main logic
5. **Don't spread global state** — no "current module" singletons without explicit context
6. **Don't over-abstract early** — each phase motivated by a real need, not hypothetical futures
7. **Don't merge Python and Node.js** — the two-language split is a security feature, not tech debt. Toolbox (credentials, MCP) stays Node.js. Cron (consumer, workflows, channels) stays Python. The language boundary prevents accidentally mixing credential-handling code with agent execution code.
8. **Don't treat loading profiles as security controls** — isolation comes from containers, process boundaries, permissions, and secret handling
9. **Don't break existing tests** — backward compatibility in every phase

---

## Success Metric

The roadmap succeeds when:

```bash
bin/agento make:module slack
# ... implement SlackChannel, SlackWorkflow, slack tools ...
# ... declare everything in one module.json ...
bin/agento config:set slack/webhook_url https://hooks.slack.com/...
bin/agento reindex
docker compose restart
# Slack integration is live — channel, workflows, tools — from one module directory
```

No framework files touched. No PR to the main repo. Just a module directory in `app/code/` with `module.json`.

---

## Security hardening backlog

- **Toolbox-only secret boundary in Python bootstrap** — `bootstrap` transiently decrypts
  DEFAULT-scope `obscure` config in the cron/consumer/CLI, so non-toolbox Python processes hold
  decrypted secrets (contradicting "Toolbox = only container with secrets"). Design + scope in
  [docs/security/toolbox-only-secret-boundary.md](docs/security/toolbox-only-secret-boundary.md).
  Surfaced by the Outlook sender-routing review (2026-07-24) as pre-existing and out of scope for
  that feature.
