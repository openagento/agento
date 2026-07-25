# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
- **Shared Outlook mailbox behavior changed (breaking for any pre-existing shared-mailbox
  deployment).** Previously a shared UPN silently collapsed to "lowest `agent_view.id` wins, others
  skipped". It is now **routed by sender**. A mailbox owned by exactly one view is unchanged
  (direct mode). If you previously hand-created inert `outlook_sender` ingress bindings, re-create
  them as regexes (`ingress:list` shows existing rows) — this is a note, not a migration.

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
