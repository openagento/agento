# Roadmap

This document expands the roadmap preview in [`README.md`](README.md).

Agento is moving quickly. The list below is directional, not a promise, and priorities may shift as the framework matures and as we learn from real deployments.

The roadmap follows one principle: **the framework provides mechanics, Core defines meaning, and Modules deliver features.** One module equals one integration equals a complete package — channel, workflows, tools, knowledge, and config in a single directory, in the Magento spirit. Two constraints hold across every milestone: the **Python + Node.js split is a security boundary** (credential-handling toolbox code never mixes with agent-execution code), and **every change stays backward-compatible** — no big-bang rewrite, existing tests pass throughout.

Contributions are welcome. Bugs, docs, polish, and tightly scoped improvements are the easiest to merge. If you want to build something bigger, the best path is usually a **module** in `app/code/` — the whole point of the architecture is that a new integration is a directory, not a patch to the framework.

Status legend: ✅ shipped &nbsp;·&nbsp; 🟡 in progress &nbsp;·&nbsp; ⚪ planned

## Milestones

### ✅ Magento-style module system

The foundation. A `modules/` directory where each module carries a `module.json` manifest and `config.json` defaults, a 3-level config fallback (ENV → DB → config.json) shared across Python and Node.js, a scoped `core_config_data` table, cross-language AES-256 encryption, and dynamic tool loading in the toolbox. The CLI (`install`, `reindex`, `module:add/list/remove`, `config:set/get/list`) drives it all.

### ✅ Core contracts & module-driven registries

Hardcoded imports gave way to a module loader that scans manifests and populates every registry — channels, workflows, runtimes — from `module.json` declarations. Core modules (jira, claude, codex) became self-contained packages, the core-vs-user split (`src/agento/modules/` vs `app/code/`) mirrors Magento's `vendor/` vs `app/code/`, and the core protocols (Channel, Workflow, Runner, Publisher) live in `framework/contracts/`. Adding an integration is a directory; removing it cleanly removes its capabilities.

### ✅ Framework kernel & scoped configuration

The `CronConfig` god-object was decomposed so every module declares its own config schema in `module.json` and has it resolved through the same 3-level fallback — Python now matches the Node.js resolver exactly for the same field and fallback chain. Framework config (database, consumer) is separated from module config, and module load order is deterministic via Magento-style `sequence` and `order`.

### ✅ Event–observer system

A Magento-style event-observer layer lets modules react to system events without touching core code — a Slack module can observe `job_fail_after` and post a notification while the Jira module and consumer never learn Slack exists. Observers are declared in `events.json`, execute in deterministic order, and a failing observer never crashes job processing. This is what turns "a codebase with plugins" into "a platform with an ecosystem."

### ✅ Core module refactoring

All business logic moved out of the framework and into core modules — the framework `cli.py` now has zero imports from any module, and modules contribute their own CLI commands and auth strategies through `di.json`. Toolbox JavaScript is co-located with each module (`<module>/toolbox/`), so one module is a complete package across both languages.

### ✅ Module setup system

Modules contribute their own schema migrations (`sql/*.sql`), data patches (`data_patch.json`), and cron jobs (`cron.json`) from their own directory — no framework files touched. A single `setup:upgrade` command applies everything in dependency order, with `--dry-run` to preview all pending work grouped by type.

### ✅ Workspace & agent-view hierarchy

First-class `workspace` and `agent_view` scopes let one installation host many agent profiles with config inheritance (`agent_view` → `workspace` → `global`). Each agent_view can override model, personality, MCP servers, and tool bindings, and agento generates the native CLI config files (`.claude.json`, `.mcp.json`, `.codex/config.toml`) from resolved scoped config before every run — no more hand-edited workspace files.

### ✅ Concurrent agent-view execution pool

A bounded worker pool runs many jobs in parallel, mixing different agent_view profiles — a `developer` on Codex, a `team-leader` on Claude Opus, a `qa-tester` on Sonnet — at the same time. Each run gets an isolated working directory with freshly generated config, priority-ordered claiming (`priority DESC, created_at ASC`) keeps urgent work ahead of bulk QA, and a failing worker fails only its own job instead of taking down the consumer.

### ✅ Ingress identities & agent resolution

Inbound Outlook, Teams, and API traffic maps deterministically to the right agent_view through a module-extensible router registry. Routing is debuggable — matched router, candidates, chosen agent_view, and reasoning are logged and emitted as events — and ambiguity is always surfaced explicitly, never resolved silently.

### ✅ Composable workspace, skills & tools

CLI-managed control over what each agent_view can do: `tool:enable`/`tool:disable`, a skills module (`skill:sync/list/enable/disable`) backed by a registry, and pre-built materialized workspaces per agent_view (`workspace:build`) so the consumer copies a ready build instead of regenerating identical files on every run. Three independent deliverables, each disableable without breaking the system.

### ✅ Versioned artifacts

Generic versioned file/directory trees: mutable **drafts**, immutable **versions**, and an atomic
**current** pointer, backed by local Git that never surfaces in the public contract. Shipped as the
core module `versioned_artifacts` (ten opt-in MCP tools plus the admin-only `artifact:init`), with a
toolbox-only storage volume, per-`agent_view` artifact allowlist, filesystem locking, crash recovery
and a `versioned_artifact_audit` trail. A separate `artifacts` container serves the published tree on
loopback, and disabling the module makes it answer 503. See
[docs/modules/versioned-artifacts.md](docs/modules/versioned-artifacts.md). Deferred: garbage
collection of Git objects (immutable versions and `audit-fallback.log` both grow without bound, though
materialized previews have an opt-in retention policy via `serving/keep_versions`),
human-in-the-loop publication approval, an annotated tag object per version so a version carries its
own label, and multiple toolbox instances per `storage_root` — which needs a real distributed lock,
see DECISIONS.md.

### 🟡 Developer experience & open-source polish

Getting a contributor from "I want to add a Slack integration" to a working module in under 30 minutes. The unified Python CLI, `doctor`, `init`, `make:module`, and `module:validate` are shipped; what remains is per-capability extension docs, architecture tests that enforce module boundaries in CI, and a log-safety audit before widening logging namespaces.

### 🟡 Event coverage & naming convention

The `{subject}_{verb}_{before|after}` naming convention is established, with event classes covering job, consumer, worker, agent_view, routing, config, setup, and migration lifecycles. Remaining events are added incrementally as later milestones introduce the features they describe (for example, tool-binding change events).

### 🟡 Composable workspace automation

Makes composable workspaces production-ready. Shipped: builds detect config drift and rebuild themselves at job-claim time (a checksum freshness check that supersedes the original dirty-flag design), and old builds are garbage-collected under a retention policy. Still pending: runtime-directory GC and a periodic `skill:sync` + `workspace:build --all` cron so skill-content changes are picked up on a schedule.

### ⚪ Admin API & Agent Studio

A minimal but real control plane so operators can create workspaces and agent_views, manage scoped config overrides, attach tools from the toolbox, and manage allowlists without hand-editing JSON or SQL. API-first and binding-based — the admin frontend is a client of the API, and the same DB source of truth backs API, CLI, and runtime.

### ⚪ Credential broker / key vault

A dedicated broker service that owns secret storage: admin writes secrets, toolbox reads them with scoped broker tokens, and agent-execution runtimes never get direct vault access by default. Config stores secret references rather than raw values, and external backends like Azure Key Vault stay optional adapters, not MVP requirements.

### ⚪ Response locale policy

Per-workspace and per-agent_view control of the language agents reply in — `preserve_input_language` or `force_output_locale` — as part of effective agent configuration, without opaque pre-translation steps. Full admin and module i18n is explicitly a later nice-to-have, not part of this milestone.

### ⚪ OAuth token pools

Group multiple tokens from the same provider into pools with capacity-based rotation and per-agent_view pool assignment, replacing direct per-provider token selection. `TokenResolver` stays the single extension point, so pool-aware selection needs no consumer changes.

### ⚪ Distribution & installation model

`agento install && agento up` should start a working system from pre-built, versioned images with no local Docker build. This decouples the lean cron image from the full agent sandbox, makes builds reproducible via lockfiles and digest-pinned base images, and separates the dev compose (bind-mounts, local builds) from the customer compose (GHCR images, data-only mounts). Independent of the other milestones — it can land in parallel with any of them.

### ⚪ Observability, telemetry & evals

Emit OpenTelemetry traces across the whole job flow — one `correlation_id`/`job_id` from ingress through runner execution, MCP tool calls, and HITL decisions — with Agento events mapped to spans. Telemetry ships as an optional module, not a hard core dependency, with any eval/observability backend (LangSmith, for example) as a pluggable exporter whose credentials stay in the cron/toolbox/collector and never reach the sandbox. On top of the traces: build eval datasets from historical production jobs and run offline/online evals as a regression gate for prompts, policies, and routing before a release.

### ⚪ Dynamic harness/model/effort routing

Today an agent_view is statically bound to a harness, model, and effort. This adds an optional pre-execution assessment step that picks harness/model/effort per job from an ordered rule set (`When → Harness → Model → Effort`, with a mandatory default row), so simple jobs run cheap and hard ones get a more capable model. Delivered as an independent `dynamic_routing` module behind a `static`/`dynamic` switch, leaving the existing static binding as the default.

### ⚪ Job threads, handoffs & shared artifacts

Group related jobs into a thread so agents can collaborate the way people do: an agent does work locally, publishes it, and hands off to another agent for review, decision, or continuation, escalating to a human when they can't agree. The core primitive is a **handoff package** — intent, origin, a work summary, and an artifact manifest — that travels between agent_views, giving a thread a shared workspace and shared, runnable artifacts instead of isolated per-job directories.

### ⚪ Areas / selective module loading — parked

Explicitly parked. There is no current justification for selective module loading, and security boundaries stay enforced by process and container separation rather than area declarations. Revisit only if module count creates measurable overhead, deployments need materially different module subsets, or selective loading solves a proven security problem better than the existing boundaries.

### ⚪ Declarative schema (`db_schema.json`) — deferred

A future declarative alternative to imperative SQL migrations: a module declares its desired tables, columns, and indexes, and `setup:upgrade` converges the actual schema to match (Magento's `db_schema.xml` equivalent). Deferred until hand-writing sequential migrations becomes a maintenance burden, or third-party modules need schema portability across database versions.

---

## Engineering notes

The milestones above are the product view. The sections below are the living operational record behind them — principles the codebase holds itself to, and specific backlog items with owners and deadlines. They are intentionally more detailed than the roadmap itself.

### Anti-patterns to avoid

1. **Don't split integrations into typed micro-modules** — one `jira` module, not `channel-jira` + `tool-jira` + `workflow-jira`.
2. **Don't copy Magento's XML hell** — `module.json` is the only manifest; keep it simple.
3. **Don't build a DI container** — Python's import system plus constructor injection is enough.
4. **Don't add an interception/plugin system** — events for cross-cutting concerns, direct code for main logic.
5. **Don't spread global state** — no "current module" singletons without explicit context.
6. **Don't over-abstract early** — each milestone is motivated by a real need, not a hypothetical future.
7. **Don't merge Python and Node.js** — the two-language split is a security feature, not tech debt. Toolbox (credentials, MCP) stays Node.js; cron (consumer, workflows, channels) stays Python. The language boundary prevents accidentally mixing credential-handling code with agent-execution code.
8. **Don't treat loading profiles as security controls** — isolation comes from containers, process boundaries, permissions, and secret handling.
9. **Don't break existing tests** — backward compatibility in every change.

### Success metric

The roadmap succeeds when this works with no framework files touched and no PR to the main repo — just a module directory in `app/code/`:

```bash
bin/agento make:module slack
# ... implement SlackChannel, SlackWorkflow, slack tools ...
# ... declare everything in one module.json ...
bin/agento config:set slack/webhook_url https://hooks.slack.com/...
bin/agento reindex
docker compose restart
# Slack integration is live — channel, workflows, tools — from one module directory
```

### Security hardening backlog

- **Toolbox-only secret boundary in Python bootstrap** — `bootstrap` transiently decrypts
  DEFAULT-scope `obscure` config in the cron/consumer/CLI, so non-toolbox Python processes hold
  decrypted secrets (contradicting "Toolbox = only container with secrets"). Design + scope in
  [docs/security/toolbox-only-secret-boundary.md](docs/security/toolbox-only-secret-boundary.md).
  Surfaced by the Outlook sender-routing review (2026-07-24) as pre-existing and out of scope for
  that feature.
- **Per-field `toolbox_only` exclusion in the config resolver** — a field only the toolbox should ever
  resolve is still resolved by `bootstrap()` from ENV (`framework/config_resolver.py:209`) for every
  enabled module, *before* any module code can run, so the only remedy available to a module is to
  refuse to operate (as `github`'s two-sided `env_guard` does on all four of its surfaces). Closing it
  properly is a framework change: let `system.json` mark a field toolbox-only and have `resolve_field`
  skip it outside the toolbox. Surfaced by the GitHub PR-review port (2026-08-14) as pre-existing and
  accepted as a residual for that port (owner sign-off 2026-08-13).
- **Internal-caller auth for the toolbox (N5-2)** — `/sse` and `/mcp` take `agent_view_id` from the
  query string with no caller authentication (`src/agento/toolbox/server.js:88,126`), and the `jira`,
  `outlook`, `bitbucket` and `github` REST handlers take it from the request body. The fix is to bind
  the view to an authenticated caller/session in `server.js` (e.g. a job-scoped token in the MCP URL
  that `server.js` resolves `agent_view_id` from), applied **once for all four modules** — a
  module-local fix would create a fourth pattern and protect nobody else. Re-confirmed by the GitHub
  PR-review port (2026-08-14), which ships at parity with the other three (owner sign-off 2026-08-13).

### Deprecation removals due next release (v0.16)

Introduced by the harness/provider split (see
[DECISIONS.md](DECISIONS.md#2026-08-04--one-closed-agentprovider-enum-split-into-harness--provider--model)).
Each is a one-release compatibility shim; remove all of them together.

| Shim | Where | Remove |
|---|---|---|
| `token:*` CLI aliases (hidden) | `framework/cli/credential_aliases.py` | delete the module + its registration |
| `--agent-type` alias on `credential:list` / `credential:usage` | `framework/cli/credential.py` | drop the hidden `add_argument` |
| `agent_type` in `--json` output alongside `scope` | `framework/cli/credential.py` | drop the duplicate key |
| `token_id` in `agent_view:prepare-run` payload alongside `credential_id` | `agent_view/src/commands/prepare_run.py` | drop the duplicate key |
| `Token*Event` payloads + `token_*` event names | `framework/events.py` (`_CREDENTIAL_EVENT_ALIASES`, `dispatch_credential_event`) | delete the alias map; dispatch once |
| `credential.agent_type` column (dual-written with `scope`) | migration | drop the column once no deployment reads it |
| Top-level `sandbox_packages` array in `di.json` | `framework/harness/manifest.py` (`_parse_legacy_sandbox_packages`) | delete the legacy parser |
| `--oauth_token` flag alias (`agento replay`, `agento e2e`) | `framework/cli/runtime.py` | drop the second flag name |
| `_iter_module_dirs` shim | `framework/cli/_provisioning.py` | callers use `framework/module_discovery.py` |
| Pre-0.15 `agent_view/provider`-as-harness fallback | `framework/agent_view_runtime._resolve_harness_and_provider` | keep until the data patch has demonstrably run everywhere; then delete the legacy branch |

### No per-job isolation inside the consumer process (raised during AG-50)

`consumer.py:204-206` runs jobs as threads in one process, and `run_preparation.py:153-162`
puts each run's credential in the directory that is also its `HOME`. One job can therefore
read another job's desk and another job's credential. Delegation — one agent scheduling
work for another — makes concurrent jobs the normal case rather than the exception, so this
moves from "theoretical" to "the default shape of a run".

### `schedule_agent_job` needs framework support, not a module workaround (raised during AG-50)

Scheduling a job for *another* agent needs a `job.type` widening plus an opened `AgentType`
(`framework/job_models.py:17-21`, `bootstrap.py:255`), and `publisher.publish()` must learn
`context` and `parent_id` (`publisher.py:84-93` writes neither). A module that
re-implements dedupe-then-insert against the `job` table instead is a second write path to
the queue.

**`schedule_followup` must not be widened to cover this.** Its idempotency key
`followup:{source}:{reference_id}:{minute}` (`schedule.js:91`) collapses a two-agent
fan-out in the same minute into one job — and reports success.

### Ungated Toolbox REST endpoints (raised during the Pi harness work)

`registerModuleRestApis` registers `POST /api/jira/request`, `/api/jira/search`,
`/api/jira/issue/comments`, `/api/bitbucket/verify` and `/api/outlook/delta` with
`isToolEnabled` **undefined** (`toolbox/config-loader.js`) — no opt-in, no agent_view
scope, no binding to a job. `/api/jira/request` is a generic Jira proxy. These are
reachable from any sandbox over the shared docker network, for **every** harness, and they
bypass the `is_enabled` allow-list that governs every MCP tool.

Not caused by the Pi work and deliberately out of its scope, but it is a real gap in the
opt-in tool model and wants its own decision: either gate them behind `isToolEnabled` like
the MCP tools, or remove them if the MCP path has superseded them.

**Raised again by AG-50 (`versioned_artifacts`):** agent_view identity on that listener is
**self-asserted** — `src/agento/toolbox/server.js:95` reads `agent_view_id` from a query
parameter supplied by a config file that lives in the job's own writable artifacts
directory. That makes it the ceiling on every per-agent_view gate, `allowed_artifacts`
included: a job that edits its own config can present any agent_view it likes. The fix is
session-bound identity in the framework, never a module-local caller check.
