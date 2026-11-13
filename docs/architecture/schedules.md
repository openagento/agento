# Schedules

A **schedule** is a row that publishes a job at the times an RRULE gives. The framework owns
the mechanism: the `schedule` table, the RRULE engine, the source registry and the
`scheduler:tick` cron command. A module gives the meaning: it registers a **schedule source**,
which says what job one fire publishes. The framework names no source (PLC).

Source files: `src/agento/framework/schedule/` (`engine.py`, `cron_convert.py`, `sources.py`,
`tick.py`), `src/agento/framework/sql/056_generic_schedule.sql`,
`src/agento/framework/cli/schedule.py`.

## The `schedule` table

Migration 056 makes the old Jira-only table generic. It renames `agent_type` → `source`,
`issue_key` → `source_ref` and `summary` → `title`, and adds:

| Column | Meaning |
| --- | --- |
| `agent_view_id` | The view the job runs in. `NULL` → the row never fires. A `scheduler` row keeps the view it was created with (docs/modules/scheduler.md). |
| `owner_user_id` / `owner_agent_view_id` | The owner. A user owner must be active and must reach the view at each fire. |
| `prompt`, `harness`, `model` | What the job carries. `harness`/`model` pin the job's runtime (`NULL` = the view's config). |
| `rrule`, `starts_at`, `timezone` | When it fires. `starts_at` is local time in `timezone`. |
| `next_fire_at`, `last_fire_at` | Naive UTC. `next_fire_at IS NULL` means "the tick computes it". |
| `anchor_local` | The local start of the current RRULE series (see below). |
| `last_error` | Why the tick disabled the row (`ended` or an engine code). |
| `deleted_at` | A soft delete. The tick removes the row. |
| `created_job_id` | The job that created the row, when an agent did. |

`cron_expr` stays, nullable, for one release. A `(agent_view_id, source_ref)` unique key
replaces the old global `issue_key` key, so two views may schedule the same issue.
`job.schedule_id` names the schedule that published a job.

## The engine

`engine.py` evaluates an RRULE with `python-dateutil`. It accepts one `RRULE` value with no
`RRULE:` prefix and only these parts: `FREQ`, `INTERVAL`, `COUNT` (≤ 10000), `UNTIL`, `BYDAY`,
`WKST`, `BYMONTH`, `BYMONTHDAY`, `BYYEARDAY`, `BYWEEKNO`, `BYSETPOS`, `BYHOUR`, `BYMINUTE`.
`SECONDLY` and `BYSECOND` are refused. Anything else raises `InvalidSchedule(code)`.

- `normalize(rrule)` → the canonical text, or `InvalidSchedule("bad_rule")`.
- `next_fires(rrule, starts_at, tz, *, after, n, anchor_local=None)` → `list[Fire(at_utc, local)]`.
  Times are wall-clock times in `tz`: a time in the spring gap moves forward, a time in the
  autumn overlap fires once (the first one).
- `validate(rrule, starts_at, tz, *, min_interval_minutes, now)` → the next 5 fires. It runs the
  evaluation in a child process (`python -P`, only `PATH` and `PYTHONPATH` in its environment)
  with a 2-second limit, so a rule that takes too long cannot block the caller. Codes:
  `bad_rule`, `too_frequent` (two fires closer than the minimum), `no_occurrence`,
  `too_complex` (time limit), `too_far_back` (the search window ends before `now`).

The search is bounded: 5 years ahead, 100000 periods at most.

**Anchor.** An RRULE counts its periods from `DTSTART`. A rule without `COUNT` is evaluated
from `anchor_local` (the last fire) when it is set, so `INTERVAL` stays in phase after a
long pause and the search stays short. A rule with `COUNT` is always evaluated from
`starts_at`, because its count must not restart. A writer that changes `rrule`, `starts_at` or
`timezone` sets `next_fire_at` and `anchor_local` to `NULL`; the tick then computes the next
fire again. A manual Run now never writes the anchor.

`cron_convert.cron_to_rrule(expr)` converts a 5-field cron expression to an RRULE for the
upgrade of old rows. It refuses `L`, `W`, `#`, `?`, `@…`, names, and a restricted day-of-month
together with a restricted day-of-week (`Unconvertible`).

## Sources

A module declares its sources in `di.json`:

```json
{"schedule_sources": [{"name": "jira_periodic", "class": "src.schedule_source.JiraPeriodicSource"}]}
```

`cron` reads the declarations from `bootstrap()`. `web` never runs `bootstrap()`, so
`routes_registry.load_module_routes` registers them there (`register_module_sources`), as it
does for channels and job types. Run now and a panel delete therefore find the source in `web`.

The class has `name`, `editable` and `fire_spec(cur, row) -> FireSpec(source, agent_type,
reference_id, thread, requester, context, priority)`. It may have `on_delete(cur, row)`, called
in the delete transaction before the row goes. `sources.load_sources()` reads the enabled
modules on each call. A broken declaration is logged and left out.

A row whose source is not registered — its module is disabled — is **not touched**: it does
not fire, it is not advanced and it is not deleted. Enable the module again and it goes on.

## The tick

`scheduler:tick` (`sc:ti`) runs every minute from the framework `cron.json`. It takes the named
lock `scheduler:tick:<database>`; when another tick holds it, it does nothing. With no source
registered it does nothing.

1. **Phase A** — at most `AGENTO_SCHEDULER_TICK_BATCH` (default 500) soft-deleted rows:
   `finalize_delete()` calls `on_delete`, deletes the row, commits, then dispatches
   `schedule_delete_after`.
2. **Phase B** — at most the same number of due rows, rows with no `next_fire_at` first:
   - no `next_fire_at`: validate the rule and set `next_fire_at`; an invalid rule sets
     `enabled = 0` and `last_error`. The child process runs with no row lock held.
   - due: `fire()`, then advance `next_fire_at` to the first fire **after now**. Missed fires
     are skipped, not caught up. A rule with no more fires sets `enabled = 0`,
     `last_error = 'ended'`.

Each row is its own transaction with one row lock. A row that raises is rolled back, logged
with its id and the exception class, and the tick goes on.

`fire(cur, schedule_id, *, fire_at, idempotency_key, actor)` is shared with Run now. It locks
the row and returns the first outcome that applies:

| Outcome | When |
| --- | --- |
| `NO_REACH` | The row is gone, deleted or disabled; its source is not registered; it has no view, or its view or workspace is inactive; or its user owner is inactive or does not reach the view. |
| `OVERLAP` | A job of this schedule is still open. |
| `TOO_SOON` | `fire_at - last_fire_at` < `core/scheduler/min_interval_minutes`. |
| `VETOED` | A `schedule_run_before` observer set `veto` or raised. |
| `PUBLISHED` | The job is inserted with `schedule_id`, `harness`, `model`; `last_fire_at` is set. |

The tick's idempotency key is `schedule:<id>:<UTC fire time>`, so a fire never publishes twice.
After the commit the caller dispatches `job_publish_after`, then `schedule_run_after`
(`RunAfter.dispatch()`). Events: [events.md](events.md#schedules).

## Database grants

The toolbox user has SELECT, INSERT, UPDATE on `schedule` (no DELETE), SELECT on
`schedule_member` and INSERT on `schedule_audit`. See [zero-trust.md](zero-trust.md#database-users).

## Upgrade checks

A module may declare `upgrade_checks` in `di.json` (`[{"name", "class"}]`). `setup:upgrade`
calls each class's `check(conn) -> list[str]` before any schema change, also with `--dry-run`.
Any message stops the upgrade with `upgrade refused, nothing was changed`, and the command
exits 1. A check must only read.
