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
    │  SELECT-then-INSERT on idempotency_key
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

The unique key on `idempotency_key` ensures the same task isn't queued twice within a time
window. The publisher checks with a `SELECT` first and only then inserts, catching the
`IntegrityError` a racing insert raises: `INSERT IGNORE` would burn an auto_increment id on
every rejected duplicate (see DECISIONS.md, AG-22).

### Two entry points, one insert

| Function | Returns | Also writes |
|---|---|---|
| `publisher.publish(...)` | `True` if inserted, `False` if duplicate | nothing |
| `publish_service.publish_job(...)` | the job **id**, existing or new | a `job.queued` row in `job_event_outbox`, **in the insert's transaction** |

A caller that must then point a row at the job (a conversation message waiting on its turn)
needs the id, and needs the announcement to commit with the job or not at all. Both share
`publisher.insert_job()`, so a job row is still born in exactly one place.

## Job Types

The four built-ins are `cron`, `todo`, `followup` and `blank`. A module may add its own —
`job.type` is a `VARCHAR(32)`, not an enum (migration `043`).

```jsonc
// <module>/di.json
{
  "job_types": ["conversation"],
  "workflows": [{ "type": "conversation", "class": "src.workflow.ConversationWorkflow" }]
}
```

- **Grammar:** lowercase letters and underscores, starting with a letter, at most 32 characters.
- **`job_types` declares; `workflows` binds.** A `workflows` entry whose `type` is a built-in
  *replaces* that built-in's workflow (`modules/jira_periodic_tasks` does this for `cron`) — it
  declares nothing. Only `job_types` adds a value to the vocabulary.
- **Collisions are refused at `module:validate`:** a built-in's value, or a value another enabled
  module already declares. The load order would otherwise decide whose workflow runs a job.
- **Unknown at runtime fails closed** with `JobTypeUnknown`, the same shape `get_channel()` raises
  for an unregistered source.
- **Compatibility:** a built-in resolves to its `AgentType` member, so `job.type == AgentType.CRON`,
  its hash and its use as a dict key are all unchanged. A module type resolves to a `ModuleJobType`
  value object with the same `.value` attribute, so `job.type.value` works for both.
- **Disabling the module removes the type.** The registry is cleared on every `bootstrap()`, which
  the consumer re-runs each poll interval when idle, so a disabled module leaves nothing behind.

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
| `DEAD` | Failed after max retries (3) — the agent exhausted its attempts |
| `FAILED` | Halted by a *blocked* verification verdict — a deterministic configuration/infrastructure fault (e.g. a missing MCP credential) that cannot heal on retry. No retry, no dead-letter; an admin alert fires instead (`job_blocked_after`). Reuses the otherwise-unused `FAILED` status so `DEAD` stays reserved for agent exhaustion. |

## Retry Policy

- **Max attempts:** 3
- **Backoff:** exponential (1 min → 5 min → 30 min)
- **Non-retryable errors** → immediately DEAD (e.g., invalid issue key)
- **Blocked verdict** → immediately `FAILED` (no retry), with an admin alert instead of a dead-letter. A verification observer sets `Verdict(blocked=True)` when the veto cause is a deterministic config/infra fault (a missing tool credential, a broken MCP config): retrying re-runs the full LLM against an unchanged world for the same result, so it stops after the first attempt and the failure is framed as a deployment fault, not an agent fault. It lands in `FAILED` (not `DEAD`) so ops can tell a config fault apart from genuine agent exhaustion.

## Concurrency & Per-Run Isolation (Phase 9.5)

The consumer runs a bounded thread pool (`AGENTO_CONSUMER_MAX_WORKERS`). Each job gets an isolated run directory with freshly generated config files (`.claude.json`, `.mcp.json`, `AGENTS.md`, `SOUL.md`), eliminating the shared-file corruption that previously forced `concurrency=1`.

**Database connections.** The consumer borrows connections from a pool (`db.pooled`). The pool keeps at most `AGENTO_CONSUMER_MAX_WORKERS` idle connections. When none is idle, it opens a new one; it never waits. The pool checks each connection at checkout and rolls it back at return. Code that runs `GET_LOCK` or `SET SESSION` must not use the pool, because both outlive the checkout (a test enforces this). The consumer renews refresh leases once per poll interval, and it runs the build freshness check at most once per poll interval for each agent_view. So a config change reaches new runs within one poll interval.

**Sizing.** `AGENTO_CONSUMER_MAX_WORKERS` stays 10 by default. To run 200 jobs at the same time, set it to 200. MySQL `max_connections` must be larger than the peak: about one connection per running job, plus the toolbox, `web` and the CLI. The compose template sets `--max-connections=600`, which is enough for 200 workers. The benchmark `tests/integration/test_orchestration_scale.py` measures connections per job and `Max_used_connections`.

Jobs are dequeued by priority: `ORDER BY priority DESC, created_at ASC`. Priority is stamped at publish time from scoped config path `agent_view/scheduling/priority` (0-100, default 50).

Each job carries `agent_view_id` (resolved via ingress routing at publish time for ingress-routed channels, or set directly by a channel's own per-agent_view publisher — e.g. the Outlook mailbox→agent_view loop). The consumer resolves the agent_view's runtime profile (provider, model, scoped config) and generates per-run config files before CLI execution.

## Consumer Configuration

Configured via environment variables (set in `docker/.cron.env` or `docker-compose.yml`):

| Env Var | Default | Description |
|---------|---------|-------------|
| `AGENTO_CONSUMER_MAX_WORKERS` | 10 | Worker pool size (max concurrent jobs). Safe under per-run isolation — which isolates *files*; the shared rotating OAuth credential is protected separately by the refresh lease (see [credentials](../cli/credentials.md)). |
| `AGENTO_CONSUMER_POLL_INTERVAL` | 5.0 | Seconds between poll cycles |
| `AGENTO_JOB_TIMEOUT_SECONDS` | 1200 | Per-**CLI-subprocess** timeout (20 min), **not** total job wall time: the runner applies it to each `proc.wait()`, and stale-job recovery skips any row with a live pid. A job that resumes or spawns more than one subprocess legitimately runs longer. |
| `DISABLE_LLM` | 0 | Dry-run mode (skip actual LLM calls) |
| `AGENTO_WORKSPACE_DIR` | /workspace | Base directory for per-run directories |

> **Naming:** Framework knobs use the `AGENTO_*` prefix so they survive the cron entrypoint's env-var whitelist (the consumer is launched through the root `launch.sh`, which starts from an empty environment). See [cron-env-contract.md](cron-env-contract.md).

## Hot-Reload

Every `AGENTO_CONSUMER_POLL_INTERVAL` (5s default), when no jobs are active, the consumer re-runs `bootstrap()` from disk + DB. `agento mo:en` / `agento mo:di`, `agento config:set`, and edits under `app/code/<name>/` apply live within one poll cycle — no container restart required.

**Caveats:**
- Python's `sys.modules` cache means edits to *core* module code (`src/agento/modules/`) require a process restart. User modules in `app/code/` re-execute on each load (via `spec_from_file_location`) and pick up edits live.
- Under `max_workers > 1` with continuous load, reload waits for an idle window (no active workers) to avoid clearing the event manager mid-dispatch.
- A `bootstrap()` cost of ~150-200ms per tick amortizes well at `poll_interval ≥ 1s`. Don't drop the interval below 1s without measuring.
- **User-module top-level side effects re-execute every reload.** `spec_from_file_location` + `exec_module` re-runs `app/code/<name>/src/*.py` each tick, so module-level network calls, file writes, or thread spawns will run every poll cycle. Keep top-level code import-only; do real initialization inside class constructors or observer `execute()` methods.

**Lifecycle events:** `module_reload_before` fires in reverse dependency order before the registry clear; `consumer_reload_after` fires after the new manifests load. Observers needing genuine shutdown semantics should subscribe to `module_shutdown_before` (fires only on real consumer shutdown), not to `module_reload_before`.

## Events

The publisher and the consumer dispatch events at each state transition. Modules can observe these via `events.json` — see [Event-Observer System](events.md).

```
job_publish_after → job_claim_after → job_succeed_after
                                    → job_fail_after → job_retry_after
                                    → job_fail_after → job_blocked_after
                                    → job_fail_after → job_dead_after
```

## Source Files

| Component | File |
|-----------|------|
| Consumer loop | [src/agento/framework/consumer.py](../../src/agento/framework/consumer.py) |
| Publisher | [src/agento/framework/publisher.py](../../src/agento/framework/publisher.py) |
| Publishing service (job id + `job.queued`) | [src/agento/framework/publish_service.py](../../src/agento/framework/publish_service.py) |
| Job models | [src/agento/framework/job_models.py](../../src/agento/framework/job_models.py) |
| Harness contract (runner, command builder, workspace adapter) | [src/agento/framework/harness/](../../src/agento/framework/harness/) |
| Event data classes | [src/agento/framework/events.py](../../src/agento/framework/events.py) |
