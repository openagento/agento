# Event-Observer System

Magento-style event-observer pattern for cross-module communication. Modules subscribe to events without direct coupling.

## How It Works

1. **Framework dispatches events** at key lifecycle points (job state changes, module loading, consumer start/stop)
2. **Modules declare observers** in `events.json` — classes with an `execute(event)` method
3. **Bootstrap wires observers** to events from each module's `events.json`
4. **Observers execute synchronously** in deterministic order (by `order` field, then name)
5. **Errors are swallowed** — a failing observer never crashes job processing

## Observer Class

```python
class MyJobFailedObserver:
    def execute(self, event):
        # event is a mutable dataclass — fields depend on event type
        logger.warning("Job %d failed: %s", event.job.id, event.error)
```

Import contracts from `agento.framework.contracts` for type hints:

```python
from agento.framework.contracts import JobFailedEvent

class MyJobFailedObserver:
    def execute(self, event: JobFailedEvent) -> None:
        ...
```

## events.json

Declare observers per event in your module's `events.json` (like Magento's `events.xml`):

```json
{
  "job_failed": [
    {
      "name": "mymodule_job_failed",
      "class": "src.observers.MyJobFailedObserver",
      "order": 100
    }
  ],
  "job_succeeded": [
    {
      "name": "mymodule_job_succeeded",
      "class": "src.observers.MyJobSucceededObserver"
    }
  ]
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique observer name (convention: `{module}_{event}`) |
| `class` | Yes | Dotted path to observer class relative to module dir |
| `order` | No | Execution priority (default 1000, lower = earlier) |

## Event Naming Convention

All event names follow a strict pattern: **`{subject}_{verb}_{before|after}`**

- **`subject`** — the entity or concept: `job`, `consumer`, `module`, `worker`, `config`, `routing`, `workspace_build`, `skill_sync`
- **`verb`** — what happens: `claim`, `fail`, `succeed`, `start`, `stop`, `save`, `load`, `resolve`
- **`before|after`** — timing relative to the action:
  - `_before` — fires before the action completes (observers can inspect but not prevent)
  - `_after` — fires after the action is committed

Examples: `job_claim_after`, `module_register_before`, `workspace_build_complete_after`

**Third-party module events** use: `{vendor}_{module}_{subject}_{verb}_{before|after}` — e.g. `acme_slack_message_send_after`. Vendor prefix prevents collisions.

## Core Events

### Job Lifecycle

| Event | Data Class | Fields | When |
|-------|-----------|--------|------|
| `job_publish_after` | `JobPublishedEvent` | `type, source, reference_id, idempotency_key, agent_view_id, priority, requester` | After job inserted into queue |
| `job_claim_after` | `JobClaimedEvent` | `job` | After job dequeued (status → RUNNING) |
| `job_succeed_after` | `JobSucceededEvent` | `job, summary, agent_type, model, elapsed_ms` | After SUCCESS commit |
| `job_fail_after` | `JobFailedEvent` | `job, error, elapsed_ms` | On any failure (fires before retry/dead) |
| `job_retry_after` | `JobRetryingEvent` | `job, error, delay_seconds, elapsed_ms` | After retry scheduled (status → TODO) |
| `job_dead_after` | `JobDeadEvent` | `job, error, elapsed_ms` | After max retries exhausted (status → DEAD) |
| `job_finalize_before` | `JobFinalizeEvent` | `job, job_result, elapsed_ms, verdict` | After `rc=0`, **before** the SUCCESS UPDATE. The mutable `verdict` field lets an observer veto a "ghost success"; **no in-tree observer sets it** — `verdict` stays `None` by default |
| `job_finalize_after` | `JobFinalizeEvent` | `job, job_result, elapsed_ms, verdict` | After the terminal status (`SUCCESS`/`TODO`/`DEAD`) commits. `verdict=None` (the in-tree default) means SUCCESS; a populated `verdict` — if a future module sets one — means the run was vetoed |

`job_fail_after` fires on every failure, then one of `job_retry_after` or `job_dead_after` also fires.

`job_finalize_before` fires after a `rc=0` run, before the SUCCESS commit. The framework's **verdict plumbing stays in place** for future modules: an observer may set `verdict` (a `Verdict` dataclass with `retryable`, `reason: VerifyReason`, `fresh_start`, `detail`); a non-`None` verdict converts the apparent success into a `JobVerificationFailed` exception that routes through the normal retry/dead path, and `verdict.fresh_start=True` additionally clears `job.session_id` so the next retry starts a fresh agent session. **No in-tree observer uses this today.** The `app_monitor` module ships `McpHealthTelemetryObserver` on this event for **telemetry only** — it records two nullable per-attempt signals (`toolbox_mcp_calls`, `toolbox_mcp_connected`) on the `job` row and optionally emails ops, but never sets a verdict and never disrupts job flow. See [src/agento/modules/app_monitor/README.md](../../src/agento/modules/app_monitor/README.md).

`JobFinalizeEvent.job_result` carries the consumer's `_JobResult`, which now propagates `RunResult.mcp_init` — the provider's CLI self-report of MCP servers visible at session start (`McpInitReport(servers=(McpServerStatus(name, status), …))`, or `None` when the provider exposes no init signal). Populating `mcp_init` is part of the runner contract: providers fill it when their CLI exposes it (Claude's `system/init` stream line does; Codex does not — see the app_monitor README).

### Consumer Lifecycle

| Event | Data Class | Fields | When |
|-------|-----------|--------|------|
| `consumer_start_after` | `ConsumerStartedEvent` | *(none)* | Consumer main loop begins |
| `consumer_stop_before` | `ConsumerStoppingEvent` | *(none)* | Consumer begins graceful shutdown |
| `consumer_reload_after` | `ConsumerReloadedEvent` | `module_count, elapsed_ms` | After per-tick hot-reload completes successfully (skipped when bootstrap raises) |

### Module Lifecycle

| Event | Data Class | Fields | When |
|-------|-----------|--------|------|
| `module_register_before` | `ModuleRegisterEvent` | `name, path, config` | Module loaded, before capabilities |
| `module_load_after` | `ModuleLoadedEvent` | `name, path` | After capabilities registered |
| `module_ready_after` | `ModuleReadyEvent` | `name, path` | All modules loaded, safe to query registries |
| `module_shutdown_before` | `ModuleShutdownEvent` | `name, path` | Graceful shutdown (reverse dependency order) |
| `module_reload_before` | `ModuleReloadEvent` | `name, path` | Per-tick consumer hot-reload (reverse dependency order). Distinct from `module_shutdown_before` — observers expecting genuine shutdown semantics must NOT subscribe here. |

### Config & Setup Lifecycle

| Event | Data Class | Fields | When |
|-------|-----------|--------|------|
| `config_save_after` | `ConfigSavedEvent` | `path, encrypted` | After CLI `config:set` commits a value |
| `setup_upgrade_before` | `SetupBeforeEvent` | `dry_run` | Before `setup:upgrade` begins work |
| `setup_upgrade_after` | `SetupCompleteEvent` | `result, dry_run` | After `setup:upgrade` finishes all work |
| `migration_apply_after` | `MigrationAppliedEvent` | `version, module, path` | After a SQL migration is applied |
| `data_patch_apply_after` | `DataPatchAppliedEvent` | `name, module` | After a data patch is applied |
| `crontab_install_after` | `CrontabInstalledEvent` | `job_count` | After crontab is updated (not on dry-run) |

### Worker Pool Lifecycle (Phase 9.5)

| Event | Data Class | Fields | When |
|-------|-----------|--------|------|
| `worker_start_after` | `WorkerStartedEvent` | `worker_slot, job_id` | Worker slot begins processing a job |
| `worker_stop_after` | `WorkerStoppedEvent` | `worker_slot, job_id, elapsed_ms` | Worker slot finishes processing |
| `agent_view_run_start_before` | `AgentViewRunStartedEvent` | `job, agent_view_id, provider, model, priority, artifacts_dir` | Before CLI execution (after config files generated) |
| `agent_view_run_finish_after` | `AgentViewRunFinishedEvent` | `job, agent_view_id, provider, model, elapsed_ms, success` | After CLI execution completes |

`agent_view_run_start_before` fires after per-job config files are generated but before the CLI subprocess starts. The `agent_view` module observes this event to write `AGENTS.md` and `SOUL.md` into the artifacts directory.

### Routing

| Event | Data Class | Fields | When |
|-------|-----------|--------|------|
| `routing_resolve_after` | `RoutingResolvedEvent` | `context, agent_view_id, matched_router, reason, candidate_count` | After routing resolves to an agent_view |
| `routing_ambiguous_after` | `RoutingAmbiguousEvent` | `context, agent_view_id, matched_router, all_routers, reason` | When multiple routers match (first wins) |
| `routing_fail_after` | `RoutingFailedEvent` | `context` | When no router matches the inbound identity |

`routing_ambiguous_after` still resolves (first router wins by order), but flags the ambiguity for observability.

### Inbound Channel Delivery

| Event | Data Class | Fields | When |
|-------|-----------|--------|------|
| `security_breach_after` | `SecurityBreachEvent` | `channel, reason, sender, reference_id, detail` | An inbound channel rejects a message as a probable spoof (e.g. an allow-listed sender failing DMARC) |
| `mailbox_stall_after` | `MailboxStalledEvent` | `channel, mailbox, reason, detail` | A shared mailbox is skipped/held by a **misconfiguration** (not a transient fault), so no mail is delivered until an operator reconciles it |

`mailbox_stall_after.reason` is one of: `policy_divergence` (shared-mailbox members disagree on admit policy — the group is not polled), `no_bindings` (routed mode with zero active `outlook_sender` bindings — mail is dropped and the cursor advances), or `upn_mismatch` (the configured mailbox UPN disagrees with the resolved mailbox — the poll is held). Both events are dispatched by the **outlook** channel and observed by `app_monitor` (`SecurityBreachAlertObserver` / `MailboxStalledAlertObserver`), which email ops when SMTP alerting is configured. Both are internal events (defined in `framework/events.py`, not re-exported from `framework/contracts/`); dispatch is fail-closed regardless of whether any observer is registered.

### Workspace Build Lifecycle (Phase 10.5a)

| Event | Data Class | Fields | When |
|-------|-----------|--------|------|
| `workspace_build_check_before` | `WorkspaceBuildCheckEvent` | `agent_view_id, error` | Consumer dispatches at job-claim time, before copying `current/` into the per-job artifacts dir. Observer rebuilds if checksum drifted; sets `error` on failure so consumer can re-raise (dispatch swallows exceptions). |
| `workspace_build_start_after` | `WorkspaceBuildStartedEvent` | `agent_view_id, build_id` | After build record inserted (status → building) |
| `workspace_build_complete_after` | `WorkspaceBuildCompletedEvent` | `agent_view_id, build_id, build_dir, checksum, skipped` | After build succeeds (status → ready) or skipped (identical checksum) |
| `workspace_build_fail_after` | `WorkspaceBuildFailedEvent` | `agent_view_id, build_id, error` | After build fails (status → failed) |

`workspace_build_complete_after` fires both for new builds and for skipped builds (when `skipped=True`, the checksum matched an existing ready build). The `skipped` flag lets observers distinguish the two cases.

`workspace_build_check_before` is the freshness gate: `execute_build` short-circuits when the resolved scoped-config checksum matches the on-disk build (~ms). It rebuilds when anything affecting the build changed — provider switch, model, skills, instructions, mcp/servers, persistent-path contract drift across `agento-core` upgrades.

### Skill Lifecycle (Phase 10.5a)

| Event | Data Class | Fields | When |
|-------|-----------|--------|------|
| `skill_sync_complete_after` | `SkillSyncCompletedEvent` | `skills_dir, new, updated, unchanged` | After `skill:sync` finishes scanning disk and updating DB |

`skill_sync_complete_after` fires after the DB commit in `sync_skills()`. Observers can use it to trigger workspace rebuilds when skill content changes (Phase 10.5b).

### Token Pool Lifecycle

| Event | Data Class | Fields | When |
|-------|-----------|--------|------|
| `token_auth_failed_after` | `TokenAuthFailedEvent` | `agent_type, token_id, error_msg, job_id` | A runtime auth failure flips a token to `status='error'` (permanent poison) |
| `token_usage_limited_after` | `TokenUsageLimitedEvent` | `agent_type, token_id, error_msg, reset_at, job_id` | A session/usage/rate limit throttles a token via `throttled_until` (temporary cooldown; `status` stays `'ok'`) |

`token_usage_limited_after` is distinct from `token_auth_failed_after`: a usage limit is **temporary**, so the consumer sets `oauth_token.throttled_until = reset_at` (a cooldown the pool skips until it passes — the token auto-recovers) and the job fails over to another healthy token, whereas an auth failure **poisons** the token (`status='error'`) until an operator or credential-refresh clears it. Both are internal events (defined in `framework/events.py`, not re-exported from `framework/contracts/`).

### Config & Setup Lifecycle

`config_save_after` fires only from CLI `config:set`, not from internal bootstrap config resolution.

`crontab_install_after` fires only when the crontab actually changed and not during dry-run.

## Event Data Mutability

Event data objects are **mutable** — observers can modify fields. Execution order is deterministic via the `order` field, so earlier observers can enrich data for later ones.

## Bootstrap Sequence

```
1. Clear all registries
2. Resolve module order (topological sort)
3. Resolve configs (3-level fallback)
4. For each module (dependency order):
   a. Load observers from events.json
   b. Dispatch module_register_before
   c. Load channels, workflows, runtimes from di.json
   d. Dispatch module_load_after
5. For each module: dispatch module_ready_after
```

Shutdown dispatches `module_shutdown_before` in **reverse** dependency order.

On consumer hot-reload (per-tick re-bootstrap when idle), `module_reload_before` fires in **reverse** dependency order before the registry clear, then `consumer_reload_after` fires once the new manifests are loaded.

## Source Files

| Component | File |
|-----------|------|
| EventManager | [src/agento/framework/event_manager.py](../../src/agento/framework/event_manager.py) |
| Event data classes | [src/agento/framework/events.py](../../src/agento/framework/events.py) |
| Bootstrap wiring | [src/agento/framework/bootstrap.py](../../src/agento/framework/bootstrap.py) |
| Consumer dispatch | [src/agento/framework/consumer.py](../../src/agento/framework/consumer.py) |
| Publisher dispatch | [src/agento/framework/publisher.py](../../src/agento/framework/publisher.py) |
| Setup dispatch | [src/agento/framework/setup.py](../../src/agento/framework/setup.py) |
| Migration dispatch | [src/agento/framework/migrate.py](../../src/agento/framework/migrate.py) |
| Data patch dispatch | [src/agento/framework/data_patch.py](../../src/agento/framework/data_patch.py) |
| CLI dispatch | [src/agento/framework/cli.py](../../src/agento/framework/cli.py) |
| Router dispatch | [src/agento/framework/router.py](../../src/agento/framework/router.py) |
| Builder dispatch | [src/agento/modules/workspace_build/src/builder.py](../../src/agento/modules/workspace_build/src/builder.py) |
| Skill registry dispatch | [src/agento/modules/skill/src/registry.py](../../src/agento/modules/skill/src/registry.py) |
| Example observers | [app/code/_example/src/observers.py](../../app/code/_example/src/observers.py) |

## When to Add an Event

- **Add** when a module might reasonably want to react to a state change (e.g., `config_save_after`, `migration_apply_after`, `job_succeed_after`).
- **Prefer `_after` events** — most events fire after the action is committed. Use `_before` only when observers need to inspect state before it changes (e.g., `consumer_stop_before`, `module_shutdown_before`).
- **Don't add** events for internal operations that modules should not interfere with (e.g., registry clearing during bootstrap).
- **Don't add** generic before/after hooks on every function — events should be at meaningful extension points.
- **Events stay synchronous** — keep debugging and ordering simple.
