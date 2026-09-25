# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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

- **Shared Outlook mailbox behavior changed (breaking for any pre-existing shared-mailbox
  deployment).** Previously a shared UPN silently collapsed to "lowest `agent_view.id` wins, others
  skipped". It is now **routed by sender**. A mailbox owned by exactly one view is unchanged
  (direct mode). If you previously hand-created inert `outlook_sender` ingress bindings, re-create
  them as regexes (`ingress:list` shows existing rows) — this is a note, not a migration.

### Fixed
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
