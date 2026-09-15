# Agento — AI Agent Framework

Use ASD-STE100 Simplified Technical English like words for communitaction.

Automates Jira tasks using AI agents (Claude Code, OpenAI Codex, Pi) in Docker containers with Magento-inspired modular architecture. The set of agents is open — see [docs/architecture/harness-contract.md](docs/architecture/harness-contract.md).

**Rules:** before you plan, write, or review a change, read [RULES.md](RULES.md). Cite rules by ID.

## What we care about most

Ranked. When two goals conflict, the higher goal wins.

1. **Zero trust** — the agent holds no tool credential. Tool secrets live only in the toolbox. (SEC)
2. **Mechanism vs meaning** — the framework is mechanism and names no vendor. Modules give meaning. (PLC)
3. **Least privilege** — every tool is declared, off unless enabled, and gated per tool. (TBX)
4. **Disableable modules** — a module can be turned off. Its dependencies are declared. (MOD)
5. **Simplicity** — the simplest thing that works. No dead code. Close the class, not the instance. (CODE, CLS)
6. **Tests and docs travel with the change.** (TST, DOC)

## Key Conventions

- **Agents:** the framework defines the **harness contract** (`CommandBuilder`, `WorkspaceAdapter`, `TranscriptReader`, `CredentialAuthenticator`, `AgentHarnessAdapter`). Agent modules implement it and register through one `agent_harnesses` entry in `di.json`. Three independent axes: **harness** (the program driving the agent) / **provider** (the model-API vendor) / **model**. See [docs/architecture/harness-contract.md](docs/architecture/harness-contract.md).
- **CLI:** `bin/agento <command>` — Magento-like CLI
- **Core modules:** `src/agento/modules/<name>/` with `module.json` — ship with framework
- **User modules:** `app/code/<name>/` with `module.json` + `config.json` — per-deployment, gitignored
- **Module dependencies:** `sequence` in `module.json` lists the modules this module needs (MOD-1).
- **Harness runtime config:** a harness reads only its own module's fields listed in `di.json` `runtime_config_fields`, as `HarnessRunContext.harness_config` and `prepare_workspace(..., harness_config=…)` (SEC-3). See [docs/architecture/harness-contract.md](docs/architecture/harness-contract.md).
- **Config:** 3-level fallback: ENV (`CONFIG__MODULE__PATH`) → DB (`core_config_data`) → `config.json`. Per-agent_view scoped config via `scope='agent_view'` in DB.
- **Config testers:** a `system.json` field may declare a `tester` (`smtp`, `http`, a toolbox probe, or `local`). Surfaced as `config:test <path>` and `t` in the admin TUI. The probe runs where the credential already lives (CFG-3). See [docs/config/testers.md](docs/config/testers.md).
- **Concurrent execution:** `AGENTO_CONSUMER_MAX_WORKERS` env var (default 10). Per-run isolation makes concurrent runs safe.
- **Consumer hot-reload:** every `AGENTO_CONSUMER_POLL_INTERVAL` (5s default) the consumer re-runs `bootstrap()` when idle — `mo:en/mo:di`, `config:set`, and `app/code/` edits apply live without restart. Caveat: edits to core module Python code (`src/agento/modules/`) still require a process restart due to `sys.modules` caching.
- **Cron container env contract:** Any env var the cron/consumer needs from `docker-compose` must use the `AGENTO_*` prefix (e.g. `AGENTO_CONSUMER_MAX_WORKERS`). The entrypoint whitelist only persists `AGENTO_*`, `MYSQL_*`, `CONFIG__*`, `TZ`, `PYTHONPATH`, `PROVIDER`, `DISABLE_LLM`, and `DISABLE_AUTOUPDATER` across the `su - agent` env wipe — non-prefixed framework knobs are silently dropped. See [docs/architecture/cron-env-contract.md](docs/architecture/cron-env-contract.md).
- **Routing:** Ingress identities map inbound requests to agent_views. Channels auto-resolve via `resolve_agent_view()` before publishing. The **Outlook** channel routes by mailbox→agent_view: a mailbox UPN owned by exactly one view is **direct mode** (the mailbox identifies the view); a UPN **shared by ≥2 views** is **routed mode** — polled once and each message routed to a view by matching the normalized sender against `outlook_sender` ingress bindings (regex `fullmatch`, highest `--priority` wins; a tie between different views is ambiguous → no job). See [docs/modules/outlook.md](docs/modules/outlook.md). Outlook reads are additionally bound to the triggering job's own message (privacy by construction), and the mailbox-enumeration tools (`outlook_search_messages`/`outlook_get_new_messages`) were removed. Enabling the **opt-in** `outlook_list_thread` tool is the **single** gate for thread read: it registers the tool *and* widens READ scope to the trigger's **own conversation only** — the thread is derived from the trusted trigger (never an agent-supplied id), and each message is authorized (allow-listed + DMARC-`pass` inbound, or physically-in-Sent-Items outbound); ACTIONS (reply/mark_processed) stay bound to the trigger. There is deliberately no second config switch. `outlook/thread_read_max_messages` (default 50, cap 200) bounds enumeration.
- **Agent view config:** Scoped DB paths `agent_view/harness`, `agent_view/provider`, `agent_view/model`, `agent_view/scheduling/priority`, `agent_view/instructions/agents_md`, `agent_view/instructions/soul_md` — resolved with agent_view → workspace → global fallback. `harness`/`provider` are `select` fields whose options come from the enabled modules' `agent_harnesses` declarations (`options_source`), not from a hardcoded list. Pre-0.15 configs that set only `agent_view/provider` (which then held the harness id) still work — see [docs/architecture/harness-contract.md](docs/architecture/harness-contract.md).
- **Security:** target model — the toolbox holds tool credentials; the agent holds no tool credential, only its own harness credential in its per-run HOME. Today's gaps (a headless agent inherits the cron env) are listed in [docs/architecture/zero-trust.md](docs/architecture/zero-trust.md#known-exceptions-and-debt). Tools and skills are opt-in: available only when `is_enabled` resolves to `1` for the scope (agent_view > workspace > default). See [docs/tools/adding-a-tool.md](docs/tools/adding-a-tool.md).
- **DB tables:** `credential` (ex-`oauth_token`) is keyed by `scope` — one credential pool per `(harness, provider)` pair that needs one.
- **Versioned artifacts:** the `versioned_artifacts` module stores file trees as drafts / immutable
  versions / an atomic `current` pointer. Git is the storage engine and **must never leak into the
  public contract** — no `repository`, `branch`, `commit`, `merge`, `rebase`, `checkout`, `worktree`
  or `ref` in a tool name, parameter, response field or error message. The store is mounted into the
  **toolbox only** (the agent container has no path to it, and no generic `git` operation on the
  store is exposed to the agent — it never receives `git(command)`), and only `service.js` may reach
  the Git backend — an import-layering test enforces that boundary. The agent owns the **whole
  lifecycle** — init → draft → version → publish → next draft — with no operator step in it:
  `artifact:init` / `artifact:list` / `artifact:publish` stay as equivalent operator interfaces.
  Creation is free, and ownership is **in the store**, not in the name: `init` writes the calling
  `agent_view_id` into an `owner` marker beside the artifact, and a caller may use what it owns plus
  whatever `allowed_artifacts` grants it — which is how one agent_view is handed another's artifact.
  An artifact with no marker is usable **only** through `allowed_artifacts`, which is what makes the
  change need no migration. The code an agent asks for is a **wish**: a taken name is answered with
  the next free `-N` (appended, never spliced over a trailing number) and the returned code is the
  identity — an operator-named code (`allowed_artifacts` or the CLI) is exempt and gets
  `ARTIFACT_ALREADY_EXISTS` instead, because renaming it would publish at an address nobody chose.
  Bounded by `limits/max_agent_artifacts` counted over the artifacts that caller may **use**, never
  over the store (a store-wide count is a cross-view cardinality oracle, and, with delete reachable
  only by an operator, a lockout of every other view until a human intervenes). Destruction is the
  one lifecycle step the agent does NOT own: `artifact:delete` removes the published tree, the store
  and the row, under the init lock and with an audit row, and there is no tool equivalent.
  `agent_view_id` is asserted by the caller on the SSE URL,
  so ownership **scopes**, it does not authorize — see ROADMAP.md. Note `save_version`, not `publish`, is the HTTP exposure boundary: every saved version
  is materialized under `published/<code>/v/<id>/` and served; `publish` only moves `current`. The agent edits a **copy**: `create_draft` /
  `materialize` write the tree onto its own workspace (the *desk*) and `save_version` copies it back,
  so no tool takes a filesystem path and there is no file-level tool on the store. A fourth
  compose service, **`artifacts`**, serves the published tree over plain `node:http` from
  `server/` (never `toolbox/`, which `config-loader.js` imports wholesale into the secrets
  container). It declares **no `networks:` key** — Compose leaves it alone on the project
  `default` network while every other service names `agento-net` — carries no
  `env_file:`/`environment:`, mounts
  `storage/versioned-artifacts/published` and `app/etc` read-only, and publishes on
  `127.0.0.1` only — one `networks:` line added for consistency would let every agent in every
  agent_view read every artifact over HTTP with `allowed_artifacts` bypassed and no audit row.
  Disabling the module must stop the serving: the server re-reads `app/etc/modules.json` per
  request and answers **503** everywhere when `versioned_artifacts` is `false`, while an absent
  file, absent key or unparseable file mean **serve** — that mirrors module enablement, not the
  `is_enabled` tool gate, which is the one that fails closed. See
  [docs/modules/versioned-artifacts.md](docs/modules/versioned-artifacts.md).
- **Setup:** `setup:upgrade` on deploy — **validates enabled module manifests first** (aborts before any DB change if a manifest is invalid, e.g. a tool missing `toolset`), then applies schema migrations, data patches, installs crontab, runs module onboarding (strict: complete, disable+dependents, or quit). Use `--skip-onboarding` for CI/CD. `bin/test` runs the same `module:validate` check. Manual alternative: pre-set config values via `config:set`. See [docs/cli/onboarding.md](docs/cli/onboarding.md).
- **Module setup files:** `sql/*.sql` (schema migrations), `data_patch.json` (data patches), `cron.json` (cron jobs), `di.json` onboarding (interactive external system setup)
- **Migration tracking:** `schema_migration` table (with `module` column), `data_patch` table
- **Events:** named `{subject}_{verb}_{before|after}`; framework and core-module event classes live in `framework/events.py`, and observers are declared in `events.json`. See [docs/architecture/events.md](docs/architecture/events.md).
- **Logs:** consumer → JSON structured, publisher/sync → text. Never delete while consumer runs.
- **Module logger output:** `get_logger(name, log_file)` also attaches its handlers to every logger named in `log.ATTACHED_NAMESPACES` (today `agento.modules.app_monitor` only), so those records persist in the same file. A namespace joins only after its `exc_info=True` sites are audited (SEC-6); see ROADMAP.md for the deferred widening.
- **Code via volume mounts (Magento-like distribution)** — every project owns a `pyproject.toml` (composer.json equivalent) pinned to `agento-core==X.Y.Z` and a per-project `.venv/` (`vendor/` equivalent). Containers bind-mount `<project>/.venv/lib/python3.12/site-packages/agento` (read-only) into `/opt/agento-src/agento`, so editing source on the host + restarting the container = instant effect (no rebuild). Native deps (cryptography, etc.) live in a container-side venv built from the project's `uv.lock`. Customer images are built locally by `agento install`/`upgrade` from the in-package context at `src/agento/framework/docker/` — no GHCR pulls. Dev compose (`docker/docker-compose.dev.yml`) uses the same thin Dockerfiles with a different build context (repo root). After source changes: `cd docker && docker compose -f docker-compose.dev.yml restart cron` (Python) or `… restart toolbox` (JS). Rebuild only for dependency changes (`pyproject.toml` / `package.json`).
- **Docker Compose split** — `docker/docker-compose.yml` is managed (regenerated on `install`/`upgrade`/`module:enable`, DO NOT EDIT). `docker/docker-compose.override.yml` is user-owned — Docker Compose auto-merges both. See [docs/deployment/docker-compose-override.md](docs/deployment/docker-compose-override.md).
- **Upgrade:** `agento upgrade` upgrades the CLI package, bumps `agento-core` in the project's `pyproject.toml`, runs `uv sync`, refreshes `.agento/docker/` build context + `docker-compose.yml`, and rebuilds local Docker images. Use `agento upgrade --version X.Y.Z` to pin a specific version, `--no-build` to skip image rebuild (CI), `--no-restart` to skip `up -d`.
- **Extensions** — three sources, all gated through `app/etc/modules.json`: (1) PyPI marketplace via `uv add <pkg>` then `agento module:enable <pkg>` — auto-resolves via `.venv/site-packages/<pkg>/`, regenerates compose with the new mount, restarts containers; (2) local under `app/code/<name>/module.json` — already mounted via `app/code:ro`; (3) drop a vendored copy into `app/code/`. Local always shadows PyPI of the same name.

## Commands

Run `uv run bin/agento` with no arguments for the full grouped command list, and `agento <command> --help` for a single command's flags. Written reference (per-command docs, flags, semantics): [docs/cli/README.md](docs/cli/README.md).

The test entry points are not part of the CLI (container restart/rebuild commands are under **Code via volume mounts** above):

```bash
bin/test                                                # all: JSON validation + Python + JS. For LLm use run AGENTO_E2E=1 bin/test
uv run pytest -q                                        # Python only (from repo root)
cd src/agento/toolbox && npm test && cd -               # JS only (vitest, from repo root)
```

## Documentation

Full developer documentation in [docs/](docs/):

- [Getting Started](docs/getting-started.md) — install + first module in 5 minutes
- [CLI Reference](docs/cli/) — all `agento` commands
- [Modules Guide](docs/modules/) — creating and managing modules
- [Config System](docs/config/) — 3-level fallback, encryption, ENV vars
- [Tool Adapters](docs/tools/) — built-in + creating custom adapters
- [Architecture](docs/architecture/) — containers, zero-trust, job queue

## Git

Working with git in this repo — commit message format, PR rules, CI-failure escalation — is described in [GIT-WORKFLOW.md](GIT-WORKFLOW.md). Follow it for every commit and PR.

## Strategic Decisions

Architectural and technical decisions (why httpx, why PyMySQL, idempotency design, etc.) are documented in [DECISIONS.md](DECISIONS.md). Add new decisions there when making non-obvious technical choices.

## Additional References

- [docker/README.md](docker/README.md) — Docker deployment, auth, Playwright setup
- [docker/cron/app/README.md](docker/cron/app/README.md) — Docker cron container internals
- [ROADMAP.md](ROADMAP.md) — framework evolution roadmap
