# Publisher-Consumer Pattern

Job queue architecture for automated task execution.

## Overview

```
Publisher (cron, every minute)
    │
    ├── jira-todo    → scan Jira for assigned TODO tasks
    ├── jira-cron    → fire scheduled recurring tasks
    └── jira-mention → detect @agent mentions in comments
    │
    ▼
MySQL (jobs table)
    │  INSERT IGNORE (idempotent)
    │
    ▼
Consumer (loop, poll every 5s)
    │  re-bootstrap from disk + DB each tick when idle
    │  SELECT FOR UPDATE SKIP LOCKED
    │
    ▼
Runner (claude -p / codex exec)
    │
    ├── SUCCESS → mark complete, comment results on Jira
    ├── TODO    → retry with backoff (1m → 5m → 30m)
    └── DEAD    → max retries exhausted, mark dead
```

## Idempotency

Each job has a unique key preventing duplicates:

```
jira:{type}:{issue_key}:{time_window|comment_id}
```

`INSERT IGNORE` ensures the same task isn't queued twice within a time window.

## Job States

```
TODO → RUNNING → SUCCESS
                → TODO (retry)
                → DEAD (max retries)
```

| State | Description |
|-------|-------------|
| `TODO` | Queued, waiting for consumer |
| `RUNNING` | Claimed by consumer, executing |
| `SUCCESS` | Completed successfully |
| `DEAD` | Failed after max retries (3) |

## Retry Policy

- **Max attempts:** 3
- **Backoff:** exponential (1 min → 5 min → 30 min)
- **Non-retryable errors** → immediately DEAD (e.g., invalid issue key)

## Concurrency & Per-Run Isolation (Phase 9.5)

The consumer runs a bounded thread pool (`AGENTO_CONSUMER_MAX_WORKERS`). Each job gets an isolated run directory with freshly generated config files (`.claude.json`, `.mcp.json`, `AGENTS.md`, `SOUL.md`), eliminating the shared-file corruption that previously forced `concurrency=1`.

Jobs are dequeued by priority: `ORDER BY priority DESC, created_at ASC`. Priority is stamped at publish time from scoped config path `agent_view/scheduling/priority` (0-100, default 50).

Each job carries `agent_view_id` (resolved via ingress routing at publish time for ingress-routed channels, or set directly by a channel's own per-agent_view publisher — e.g. the Outlook mailbox→agent_view loop). The consumer resolves the agent_view's runtime profile (provider, model, scoped config) and generates per-run config files before CLI execution.

## Consumer Configuration

Configured via environment variables (set in `docker/.cron.env` or `docker-compose.yml`):

| Env Var | Default | Description |
|---------|---------|-------------|
| `AGENTO_CONSUMER_MAX_WORKERS` | 10 | Worker pool size (max concurrent jobs). Safe under per-run isolation. |
| `AGENTO_CONSUMER_POLL_INTERVAL` | 5.0 | Seconds between poll cycles |
| `AGENTO_JOB_TIMEOUT_SECONDS` | 1200 | Max job runtime (20 min) |
| `DISABLE_LLM` | 0 | Dry-run mode (skip actual LLM calls) |
| `AGENTO_WORKSPACE_DIR` | /workspace | Base directory for per-run directories |

> **Naming:** Framework knobs use the `AGENTO_*` prefix so they survive the cron entrypoint's env-var whitelist (the consumer is launched via `su - agent`, which wipes the parent env). See [cron-env-contract.md](cron-env-contract.md).

## Hot-Reload

Every `AGENTO_CONSUMER_POLL_INTERVAL` (5s default), when no jobs are active, the consumer re-runs `bootstrap()` from disk + DB. `agento mo:en` / `agento mo:di`, `agento config:set`, and edits under `app/code/<vendor>/<name>/` apply live within one poll cycle — no container restart required.

**Caveats:**
- Python's `sys.modules` cache means edits to *core* module code (`src/agento/modules/`) require a process restart. User modules in `app/code/` re-execute on each load (via `spec_from_file_location`) and pick up edits live.
- Under `max_workers > 1` with continuous load, reload waits for an idle window (no active workers) to avoid clearing the event manager mid-dispatch.
- A `bootstrap()` cost of ~150-200ms per tick amortizes well at `poll_interval ≥ 1s`. Don't drop the interval below 1s without measuring.
- **User-module top-level side effects re-execute every reload.** `spec_from_file_location` + `exec_module` re-runs `app/code/<vendor>/<name>/src/*.py` each tick, so module-level network calls, file writes, or thread spawns will run every poll cycle. Keep top-level code import-only; do real initialization inside class constructors or observer `execute()` methods.

**Lifecycle events:** `module_reload_before` fires in reverse dependency order before the registry clear; `consumer_reload_after` fires after the new manifests load. Observers needing genuine shutdown semantics should subscribe to `module_shutdown_before` (fires only on real consumer shutdown), not to `module_reload_before`.

## Events

The consumer dispatches events at each state transition. Modules can observe these via `events.json` — see [Event-Observer System](events.md).

```
job_published  → job_claimed → job_succeeded
                             → job_failed → job_retrying
                             → job_failed → job_dead
```

## Source Files

| Component | File |
|-----------|------|
| Consumer loop | [src/agento/framework/consumer.py](../../src/agento/framework/consumer.py) |
| Publisher | [src/agento/framework/publisher.py](../../src/agento/framework/publisher.py) |
| Job models | [src/agento/framework/job_models.py](../../src/agento/framework/job_models.py) |
| Harness contract (runner, command builder, workspace adapter) | [src/agento/framework/harness/](../../src/agento/framework/harness/) |
| Event data classes | [src/agento/framework/events.py](../../src/agento/framework/events.py) |
