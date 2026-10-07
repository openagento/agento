# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
- **ACL resources declared by modules.** `di.json` `acl_resources: [{id, title}]` makes an
  operation grantable (`grant:add --operation`, the panel role page); `admin` has all. The
  conversation module declares `conversation.run_details`: prompts, tool input and output, harness,
  provider, credential label, model, tokens and job links are shown to a non-admin only with this
  grant. Each attempt records its harness, provider, model and credential id at mint
  (`RunProfile`, migration `conversation/003`), so a failed run shows them too.
- A thread with no title takes one from its first message. `run.started` carries `max_attempts`.
- RULES.md SCL-1: the target is 100–200 parallel jobs per deployment.
- RULES.md UI-7: a row, header or copy action is an icon with a hover title (`IconAction`,
  `ArchiveAction`); `CopyButton` and the panel's row actions are icons now.

### Changed
- `GET /api/conversation/threads/{id}` adds `run_details`; without it, `runs` rows omit `model`,
  tokens, `job_id`, `type` and `agent_type` (they were shown to every reader before). With it,
  `agent_type` is replaced by `harness`, `provider` and `credential`.
- `accounts.GRANTABLE_OPERATIONS` is replaced by `accounts.grantable_operations()`.

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

### Added
- **`agento run <code> --yolo`** — opt-in bypass mode for interactive sessions. With `--yolo` the
  agent CLI skips its per-action approval prompts, the same bypass headless jobs always use (Claude
  `--dangerously-skip-permissions`, Codex `--dangerously-bypass-approvals-and-sandbox`). Without it,
  interactive keeps the CLI's normal approval prompting. The flag works in either position
  (`run dev --yolo` or `run --yolo dev`) and is a no-op for headless (always bypass). Provider-agnostic:
  `CliInvoker.interactive_command()` gained a `yolo` keyword; each agent module decides its own flag.

### Changed
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

### Fixed
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
