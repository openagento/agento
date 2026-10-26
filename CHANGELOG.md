# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Limits right after a re-login.** The `core` observer `core_refresh_credential_limits` reads a
  credential's limits on `credential_register_after` and `credential_refresh_after`, so the panel
  shows them at once, not on the next `credential:limits` tick. The re-login dialog shows an
  animated success mark and reads the credential list again when it closes.
- **Live limits.** `credential:limits` runs every minute (was every 10 minutes). The Credentials
  page reads the list again every minute, with no reload, and marks limits not read for 5
  minutes "Stale". New rule UI-8: the panel refreshes data itself, never by a reload.
- **Refresh outside the job (AG-65).** A near-expiry OAuth credential (Claude, Codex) is now
  refreshed out of band by the new `WorkspaceAdapter.refresh_credentials` before the job runs,
  and its lease is released at once, so the pool is exclusive for seconds instead of a job's
  lifetime. Releasing a lease wakes the jobs deferred with `CredentialsBusyError`. The refresh
  persists an access-token or expiry change too, not only a new refresh token.
  Codex forces the access-token JWT `exp` into the past in the throw-away HOME, because Codex
  0.157 refreshes at startup only on an expired JWT. A refused refresh token ("could not be
  refreshed", Claude 2.1.291 and Codex) is now a transient auth error, not an unclassified one.
- **Apps as tiles, with rights per app** (D-MINIAPP-3). `/apps` shows every app the user may see as
  tiles (default) or a list. Each app has `view`, `edit`, `remove` and `execute`: admin has all of
  them; the panel user whose conversation made the app has `view`, `edit` and `remove`; members (a
  user or a role, added on the new app settings page `/apps/<code>`) have `view` plus the flags they
  get. `execute` is computed: an approval in the view, and `artifact.launch` plus a tool grant for
  every action of the manifest (admin needs no grant). A view-only tile is disabled. An app has a
  title and an icon (a name from a fixed set). Delete and revoke ask first. See
  [docs/modules/miniapps.md](docs/modules/miniapps.md#rights).
- **`acl.json`: panel leaves and operations per module** (D-ADMIN-ACL-2). The framework and each
  module declare their admin screens, leaves and scoped operations in `acl.json`, the Magento
  `acl.xml`. Every panel route names an `acl` (a leaf, a list of leaves, `self` or `anonymous`); a
  route with a missing or unknown `acl` fails `module:validate` and stops `web` at start. A leaf of a
  disabled module leaves the role tree. The artifact leaves are now `versioned_artifacts`,
  `versioned_artifacts.publish` and `versioned_artifacts.delete`; a miniapp preview is
  `POST /api/miniapps/previews` (a `preview` key on `/api/miniapps/launches` is `400`). See
  [docs/modules/README.md](docs/modules/README.md#acljson).
- **Admin panel access per role.** The role page has a third scope, **Admin panel (everywhere)**,
  with one leaf per admin screen (Dashboard, Jobs, Agents, Credentials, Tools, Skills, Config, Users,
  Roles) and one per action (view, create, edit, delete). A role sees and calls only the
  screens and actions it holds; `admin` holds them all. A role grants only leaves it holds, and only
  `admin` touches an admin account. `grant:add --operation admin.users --role support` adds one with
  no scope. See
  [docs/architecture/panel.md](docs/architecture/panel.md#roles-and-grants).
- **The panel Test button runs `local` testers** (for example `agent_view/model` and the SSH
  identity tester). `web` runs them in its own process, as `config:test` does in `cron`, with the
  same redacted result codes. `web` registers the harnesses of the enabled modules again when
  `modules.json` changes, so `module:enable` needs no `web` restart. `web` now mounts every
  `runner-<i>-sock` read-only, like `cron`. See [docs/config/testers.md](docs/config/testers.md).
- **One in-attempt retry after a runner crash.** When a run fails with an unclassified crash and
  has a session id, and the harness can resume, the consumer resumes it once in the same attempt,
  with the same credential, lease and HOME. A paused job is never retried. A failure of the retry
  takes the same path as a first failure (poison, throttle, redaction). Log line: `process_retry`.
- **`AUTH_REQUIRED` wait for an exhausted credential pool.** A job whose pool has no enabled or
  healthy credential now waits (`TODO`, attempt refunded, `job.error_class =
  'CredentialsUnavailableError'`) and tries again after 15 minutes, instead of going `DEAD`. A job
  older than 24 hours goes `DEAD` as before. A new or refreshed credential pulls up to 200 waiting
  jobs forward. New index `idx_job_auth_wait` (migration `052_job_auth_wait_index`). See
  [docs/architecture/publisher-consumer.md](docs/architecture/publisher-consumer.md).
- **Per-run uid in the runner.** Each run starts as its own uid from a fixed range per runner
  (20000 + 256 per runner), so one run cannot read another run's files or capability. The runner
  server runs as root with a small capability set and runs no harness code; a worker process runs
  the harness as the run uid. The run dir is shared by group with the toolbox, which now drops to
  `agent` with every run gid. The Linux live acceptance is still open (ROADMAP.md). See
  [docs/architecture/runner.md](docs/architecture/runner.md).
- **Harness native-config passthrough per agent_view (AG-27).** Three new config fields hand each
  harness a blob of its own native config, deep-merged over the block Agento generates (operator
  wins), so no new Agento code is needed per CLI setting: `claude/settings` (JSON →
  `.claude/settings.json`, e.g. `{"permissions":{"deny":["WebSearch","WebFetch"]}}` or
  `{"advisorModel":"fable"}`), `codex/config` (TOML → `.codex/config.toml`) and `pi/settings`
  (JSON → `$HOME/.pi/agent/settings.json`). An invalid blob fails the workspace build. Never put a
  credential in them — they are stored unencrypted and written into the agent container. See
  [docs/modules/claude.md](docs/modules/claude.md), [codex.md](docs/modules/codex.md),
  [pi.md](docs/modules/pi.md).
  In `agento admin` each of those fields is listed on the **agent_view** node, beside the harness
  selector, and only for the harness this view uses — a `system.json` field asks for that with
  `"harness_option": true`, the same shape as `provider_option` one axis up. The config path is
  unchanged (`{module}/{field}`), so `config:set` and `runtime_config_fields` are untouched.
- **Network split.** `agento-net` is replaced by `db-net` (mysql, cron, web, toolbox),
  `exec-net` (runners, toolbox, proxy) and `web-net` (proxy, web). No agent container
  reaches MySQL, `web` or `cron` over the network. **Upgrade:** a `docker-compose.override.yml`
  that names `agento-net` must use `exec-net` or `db-net`
  ([override docs](docs/deployment/docker-compose-override.md)). New smoke:
  `docker/smoke/runner-net-smoke.sh`. A fresh MySQL now gives the migration user its grant on
  both name forms, so the first `setup:upgrade` does not fail with MySQL 1142.
- **Runner service.** Every agent CLI now starts in a `runner-<i>` compose service (cron image,
  no `env_file`, no `MYSQL_*`, no key), not in `cron`. The consumer sends each run over a Unix
  socket (`framework/runner/`); codex login, the model checks, the panel re-login and the
  attended `credential:register` login go there too. Set the number with
  `AGENTO_RUNNER_COUNT` in `docker/.env` (default 1) and re-render the compose; **drain first** before you lower it. Migration `051_job_runner_ref`
  adds `job.runner_ref`: stale recovery, resume and pause ask the runner that owns the run, and
  a runner that does not answer blocks recovery instead of a guess. See
  [docs/architecture/runner.md](docs/architecture/runner.md).
- **Panel roles are rows (Users → Roles).** A `role` table (migration `050_role`) replaces the
  hardcoded `admin`/`user` pair; both stay built in. An admin creates, renames and deletes roles,
  and edits a role's tools and operations per scope as a checkbox tree saved by one
  `PUT /api/admin/roles/{code}/resources`. CLI `role:list` / `role:create` / `role:delete`
  (`ro:li`, `ro:cr`, `ro:de`); `--role` takes any code in the table.
- **Chat UX for conversations (E9).** Claude (`--include-partial-messages`) and pi stream text and
  reasoning live: new fragment kinds `assistant.partial`, `reasoning.partial` and
  `assistant.reasoning`, coalesced per run (one row per 250 ms or 4 KiB, at most 4 writer commits
  a second) and proven at 250 parallel runs. Codex passes `-c model_reasoning_summary=auto` and
  shows its reasoning summary. The panel shows the chat layout, tool summaries, one status line,
  one error per turn with a retry, collapsed reasoning, code highlight and a phone drawer.
- **ACL resources declared by modules.** A module's `acl.json` `operations: [{id, title}]` makes an
  operation grantable (`grant:add --operation`, the panel role page); `admin` has all. The
  conversation module declares `conversation.run_details`: prompts, tool input and output, harness,
  provider, credential label, model, tokens and job links are shown to a non-admin only with this
  grant. Each attempt records its harness, provider, model and credential id at mint
  (`RunProfile`, migration `conversation/003`), so a failed run shows them too.
- A thread with no title takes one from its first message. `run.started` carries `max_attempts`.
- RULES.md SCL-1: the target is 100–200 parallel jobs per deployment.
- RULES.md UI-7: a row, header or copy action is an icon with a hover title (`IconAction`,
  `ArchiveAction`); `CopyButton` and the panel's row actions are icons now.

- **Regex + priority sender routing for shared Outlook mailboxes.** A mailbox UPN shared by two or
  more agent_views is now polled once and each message routed to a view by matching the normalized
  sender against `outlook_sender` ingress bindings (case-insensitive regex `fullmatch`, highest
  `priority` wins; a tie between different views is ambiguous → no job). Configure with
  `agento ingress:bind outlook_sender '<regex>' <view> --priority <n>`.
- `ingress:bind --priority` and an `ingress_identity.priority` column (migration 029).
- Generic `di.json` capability `regex_identity_types` for modules to declare regex-matched ingress
  identity types.
- `mailbox_stall_after` event (`MailboxStalledEvent`) — dispatched when a shared Outlook mailbox
  stops delivering mail because of a **misconfiguration** (`policy_divergence` / `no_bindings` /
  `upn_mismatch`), so the condition is otherwise visible only in the log. `app_monitor`'s new
  `MailboxStalledAlertObserver` emails ops (when SMTP alerting is configured), mirroring
  `security_breach_after`.
- Dependency: the `regex` module (bounded per-match timeout for admin-authored ingress patterns).

- **`agento run <code> --yolo`** — opt-in bypass mode for interactive sessions. With `--yolo` the
  agent CLI skips its per-action approval prompts, the same bypass headless jobs use by default (Claude
  `--dangerously-skip-permissions`, Codex `--dangerously-bypass-approvals-and-sandbox`). Without it,
  interactive keeps the CLI's normal approval prompting. The flag works in either position
  (`run dev --yolo` or `run --yolo dev`) and is a no-op for headless (which bypasses by default).
  Provider-agnostic:
  `CliInvoker.interactive_command()` gained a `yolo` keyword; each agent module decides its own flag.

### Changed
- **`credential:limits` renews an expired OAuth token.** An idle Claude or Codex credential no
  longer shows frozen limits: the cron takes the refresh lease a job takes, refreshes the token
  through the harness's `refresh_credentials` (AG-65), releases the lease, then reads the limits.
  A held lease or a failed refresh keeps the last result, as before.
- **Miniapps: the approval is no longer the grant.** The toolbox `launch` checker reads the role's
  tool grants again: a non-admin runs an app only when the role holds a grant for every action of
  the approved manifest. **Upgrade:** grant the manifest tools to each role that runs apps
  (`grant:add --role <role> --tool <action> --agent-view <view>`), add members to each app, and
  approve each app again (an approval made before this release stores no manifest, so it gives no
  `execute` to a non-admin). A launch without `execute` is now `403` (was `404`). Migrations:
  miniapps `003_miniapp_settings`, versioned_artifacts `006_artifact_created_job`.
- **Miniapps: one catalogue route.** `GET /api/miniapps/agent-views/<id>/catalogue` is replaced by
  `GET /api/miniapps/apps` (all views); a preview needs `edit` on the app.
- **The backend is trusted (D-BACKEND-2).** `cron` and `web` may decrypt what they need; the trust
  boundary is the runner. The planned keyholder service is dropped. RULES.md and AGENTS.md goal 1
  and SEC-1 say so, and zero-trust.md marks the backend rows `Part of the model`. Tool secrets
  still live only in the toolbox.
- **Deprecated:** the admin TUI (`agento admin`). Use the admin panel. The TUI stays in v1.0.0
  and is removed after it; the CLI commands stay. The command writes a deprecation line to stderr,
  and [docs/cli/admin.md](docs/cli/admin.md) gives the CLI command for each action that the panel
  does not do.
- **Lifecycle events reach a thread in about 1 s, not up to 60 s.** `conversation:relay` now loops
  inside one cron run (one pass per second, for 55 s). One invocation holds the lock
  `conversation_relay`; a second one exits at once, so there is still only one relay.
- **`web` and `cron` are one trusted backend (D-BACKEND-1).** `web` now gets the same
  `env_file` as `cron`, so it holds `AGENTO_ENCRYPTION_KEY`; both use the `agento_backend` DB
  user. A compromise of `web` now exposes the key. The service names do not change. A panel
  re-login encrypts the pasted code with the key; the per-login RSA key pair is gone and
  `credential_login.code_key` is no longer written. Only the `runner-<i>` services hold no
  secret.
- **Least-privilege DB users.** `cron_agent` is now the migration user; only `setup:upgrade`
  uses it. `cron` and `web` run as `agento_backend` (DML only), the toolbox as `agento_toolbox`
  (only the tables its SQL uses, no `credential`). `setup:upgrade` makes the grants again on each
  run. The passwords (`MYSQL_BACKEND_PASSWORD`, `MYSQL_TOOLBOX_PASSWORD`) are generated into
  `docker/.env`. **Upgrade:** run `agento upgrade` without `--no-restart` once; it runs the
  one-time root grant. MySQL now publishes its port on `127.0.0.1` only
  ([upgrade.md](docs/cli/upgrade.md#database-users)).
- **`web` runs on FastAPI and uvicorn.** The routes, the di.json regex contract and the SEC-12
  order do not change. There is no OpenAPI surface. Pydantic is installed as a FastAPI
  dependency only; our code does not import it.
- **Harness runner contract (WS5).** `SubprocessRunner` no longer writes usage
  (`_record_usage` and `_get_db_connection` are gone): it calls `observe(on_usage=…)`, and the
  worker writes the row. Its child starts in a new session, and it keeps the last 4 MiB of each
  stream. `HarnessRunContext` gets `tag` (`job:<id>`). A third-party harness that spawns a vendor
  CLI itself must use `runner.client.run` / `.pty`.
- `GET /api/conversation/threads/{id}` adds `run_details`; without it, `runs` rows omit `model`,
  tokens, `job_id`, `type` and `agent_type` (they were shown to every reader before). With it,
  `agent_type` is replaced by `harness`, `provider` and `credential`.
- `accounts.GRANTABLE_OPERATIONS` is replaced by `accounts.grantable_operations()`.
- **Faster job start at 100–200 parallel jobs.** The consumer and its per-job observers borrow
  database connections from a pool (`db.pooled`, at most `AGENTO_CONSUMER_MAX_WORKERS` idle).
  Refresh leases are renewed once per poll interval, not once per claim. The build freshness
  check runs at most once per poll interval for each agent_view, so a config change reaches new
  runs within one poll interval. Benchmark at 100 jobs: 13 → 1.1 connections per job,
  claim → spawn p95 0.25 s → 0.16 s. Compose and CI set MySQL `max_connections` to 600.
- `job.toolbox_mcp_calls` now counts the tool calls the toolbox audited (`tool_invocation`) on the
  attempt's `mcp_job` capability. No transcript is parsed: the Claude and Codex transcript readers
  and the app_monitor parser-drift log are removed. A job with no agent_view records `NULL`.
- **Deprecated:** `TranscriptReader`, `ToolUse`, `ParseSummary` and the adapter member
  `transcript_reader`. The harness contract has 4 members now; the framework does not read
  `transcript_reader`. The names stay importable until v0.18 (ROADMAP.md).
- `GET /api/conversation/threads/{id}` adds `run_details`; without it, `runs` rows omit `model`,
  tokens, `job_id`, `type` and `agent_type` (they were shown to every reader before). With it,
  `agent_type` is replaced by `harness`, `provider` and `credential`.
- `accounts.GRANTABLE_OPERATIONS` is replaced by `accounts.grantable_operations()`.
- **Codex CLI pinned to 0.157.0** (was 0.137.0) in the sandbox image, the dev compose build arg and
  the `codex` harness declaration's `default_range`.
- **BREAKING — an invalid `agent_view/claude/permissions` now fails the workspace build.** It used
  to be logged and skipped, which is a deny-list the operator believes is in place and is not.
  Remedy: `agento config:get agent_view/claude/permissions --scope=agent_view --scope-id=<id>`, then
  fix the JSON or unset it. The value also now lands in `.claude/settings.json` instead of
  `.claude.json`, where Claude ignored it.
- **BREAKING — `agento replay <job_id>` refuses a job whose `agent_view_id` is NULL.** The harness's
  own config, and therefore the exact command, cannot be resolved for such a row (pre-0.15 jobs and
  stubs), and a replay that silently differs from the run it claims to reproduce is worse than none.
- Codex headless runs omit `--dangerously-bypass-approvals-and-sandbox` when `codex/config` sets
  `sandbox_mode` — that flag overrides `config.toml`, so without this the operator's sandbox would
  be silently dead. With no blob, behaviour is unchanged.

- **The final answer is the last assistant message, for every harness.** `job.output` (and
  what a channel posts back) is now the text of the last assistant message. **Codex:** earlier
  `agent_message` items are progress notes and are no longer joined into the answer, so a Jira
  comment holds the answer only. **Claude:** `job.output` is the `result` text, not the full
  stream-json (a run with no `result` still keeps the raw stream). **Pi:** user and tool text
  no longer leaks into the answer. Each harness also ships a `stream_event_mapper` for the live
  timeline (`docs/modules/{claude,codex,pi}.md`).
- **Shared Outlook mailbox behavior changed (breaking for any pre-existing shared-mailbox
  deployment).** Previously a shared UPN silently collapsed to "lowest `agent_view.id` wins, others
  skipped". It is now **routed by sender**. A mailbox owned by exactly one view is unchanged
  (direct mode). If you previously hand-created inert `outlook_sender` ingress bindings, re-create
  them as regexes (`ingress:list` shows existing rows) — this is a note, not a migration.

### Removed
- **The `miniapps` and `miniapps.approve` leaves** and `miniapps/acl.json`. The per-app rights
  replace them; an existing `role_grant` row for them grants nothing.
- **The idle `sandbox` compose service.** The sandbox image stays (the cron image builds on it).
  `agento run` and `agento doctor` exec into `runner-1`. **Upgrade:** a
  `docker-compose.override.yml` that names `sandbox:` makes a service with no image; remove that
  block.
- **`execution.harness_session_id`** (never read or written). `job.session_id` is the one session
  store. Migration `conversation/005_drop_harness_session_id.sql`.
- **The same-uid hardening stack in `cron`.** No agent runs in `cron` now (the runner runs
  them), so `drop.py`, `split-env.py`, `framework/store_env.py`,
  `framework/credential_store_env.py`, `launch.sh --store` and the CLI's `make_non_dumpable()`
  are gone. The entrypoint writes one root-only env file (same whitelist); the consumer reads
  `MYSQL_*` and the key from `os.environ`. `app/code` in `cron` is trusted operator code. See
  [zero-trust.md](docs/architecture/zero-trust.md#what-the-runner-split-replaced).
- **Compatibility shims due in v0.16 (ROADMAP.md).** Use the new form before you upgrade:
  - `token:*` commands and `to:*` shortcuts: use `credential:*`.
  - `token_*_after` events and their `Token*Event` payloads (also gone from
    `framework.contracts`): bind the `credential_*_after` events and read `scope` /
    `credential_id`. An observer on a `token_*` name now gets no event.
  - `--agent-type` on `credential:list` / `credential:usage`: use `--scope`.
  - `agent_type` in `credential:list --json`: read `scope`.
  - `token_id` in the `agent_view:prepare-run` payload: read `credential_id`.
  - `--oauth_token` on `agento replay` / `agento e2e`: use `--credential`.
  - `--pass <p>` on `artifact:auth`: use `--pass-stdin`.
  - The top-level `sandbox_packages` array in `di.json` is ignored: move each entry to
    `agent_harnesses[].sandbox_package`.
  - `ConsumerConfig.concurrency`: use `max_workers`.
  - The private `_iter_module_dirs` in `framework/cli/_provisioning.py`: use
    `framework.module_discovery.iter_module_dirs`.
- Still open: the `credential.agent_type` column and the pre-0.15 `agent_view/provider`
  fallback (ROADMAP.md).

### Fixed
- **Cron image builds again with CPython 3.15 released (AG-73).** The cron Dockerfile ran `uv venv`
  with no Python pin, so uv took the newest managed CPython (3.15). `regex`, `pydantic-core` and
  `cffi` have no cp315 wheels, and the build failed with `No such file or directory: 'cc'`. The
  Dockerfile now sets `ENV UV_PYTHON=3.12`, and `agento install` writes
  `requires-python = ">=3.12,<3.15"` into a new project's `pyproject.toml`.
- **Large-result offload files no longer overwrite each other.** `large-result.js` named the file
  `result_YYYYMMDD_HHMMSS.{csv,txt}`, so two calls of the same tool that ended in the same second
  wrote the same path and the second result replaced the first. The name now has a random 8-hex
  suffix: `result_YYYYMMDD_HHMMSS_<hex>.{csv,txt}` (AG-66).
- **MSSQL tools now honour `core/sql_timeout_seconds` (AG-68).** node-mssql ignores
  `Request.timeout` and reads only `requestTimeout` from the pool config, so every MSSQL query
  failed at the library default of 15 s. The adapter now passes the configured timeout as the pool
  `requestTimeout`. The healthcheck keeps its own 10 s limit through request cancellation.
- **Conversation follow-ups on claude, codex and pi.** The next turn of a thread lost the new
  message on claude and codex (the resume sent a fixed "continue" text), could not find the
  session on claude (it is filed under the earlier job's run dir: "No conversation found"),
  and silently started an empty session on pi. The runner now moves the session into the new
  run dir; a session that is gone starts a fresh one with the whole thread.
- A claude error with no `result` text now shows claude's `errors[]` instead of "unknown error".
- The panel stream badge no longer sticks on RECONNECTING after a run's error event.
- **Jobs no longer dead-letter on `401 OAuth access token has been revoked` while healthy tokens sit
  unused in the pool.** The message previously matched no known phrase, degraded to a generic
  `RuntimeError`, and so never poisoned or throttled the token nor set `retry_with_other_token` —
  leaving the deterministic `ORDER BY priority ASC` token selection to hand back the *same* broken
  credential on every retry (observed in production on two separate jobs: 3 identical attempts →
  `DEAD`). Revoked/stale-token rejections are now classified as a new **transient auth** category:
  the token gets a 15-minute `throttled_until` cooldown (**not** `status='error'` — it is usually
  still serving other jobs) and the job fails over to the next healthy token, dead-lettering only
  once the pool is genuinely exhausted. Both the Claude and Codex providers classify revoked/stale-token
  wording; Claude additionally classifies **unrecognised** `401` credential-rejection wording as
  transient, so a future change to the CLI's error text fails over instead of silently retrying the
  same credential.
- New `token_auth_throttled_after` event (`TokenAuthThrottledEvent`) — dispatched when a transient
  auth failure throttles a token rather than poisoning it.

### Known issues
- The underlying credential-refresh race at `AGENTO_CONSUMER_MAX_WORKERS > 1` (a concurrent job
  rotates a token while another attempt is holding an older copy) is unchanged. The transient-auth
  category makes it survivable rather than fatal; closing the race itself is tracked separately.

## [0.1.0] - 2024-01-01

### Added
- Core framework with Magento-inspired modular architecture
- Job queue consumer with MySQL backend
- Node.js toolbox MCP server (zero-trust credential broker)
- Sandbox container for Claude Code and OpenAI Codex
- Module system: core modules (jira, claude, codex, core, crypt, agent_view)
- 3-level config fallback (ENV, DB, config.json)
- Event-observer system with module-scoped events
- CLI: `bin/agento` with install, reindex, module management, config, tokens
- Setup lifecycle: `setup:upgrade` with schema migrations, data patches, crontab
- AES-256-CBC encryption for obscure config fields
- Ingress identity routing for multi-agent-view support
- Docker Compose deployment with three-container architecture
