# Jira channel

The `jira` module publishes work from Jira Cloud (assigned TODO issues, follow-ups on an issue) and
ships the `jira_*` toolbox tools. The `jira_periodic_tasks` module (it needs `jira`) schedules the
`cron` jobs of periodic issues. This page covers periodic tasks, session continuity and attachments; the config fields are in
each module's `system.json`.

## Periodic tasks

`jira:periodic:sync` (`ji:pe:sy`, every hour from the module's `cron.json`) reads the issues in the
periodic status (`jira_periodic_tasks/jira_status`) of each active agent_view. For each issue it
writes one `schedule` row of source `jira_periodic`, owned by that view: `source_ref` = the issue
key, `title` = the summary, `rrule` = the `frequency_map` value of the issue's Frequency label.
An issue that left the status has its row disabled. The framework's `scheduler:tick` fires the
rows: each fire publishes a `cron` job of source `jira` on the issue's thread `jira:<KEY>`
([schedules.md](../architecture/schedules.md)). A missed fire is skipped. A fire that comes while
the previous job of the row is still open is skipped too; it does not run later.

`jira_periodic_tasks/frequency_map` maps a Jira Frequency label to an RRULE (no `RRULE:` prefix):

```bash
agento config:set jira_periodic_tasks/frequency_map \
  '{"Daily at 8:00":"FREQ=DAILY;BYHOUR=8;BYMINUTE=0","Weekly (Mon, 7:00)":"FREQ=WEEKLY;BYDAY=MO;BYHOUR=7;BYMINUTE=0"}'
```

The times are local in the zone of the row. Sync (and the upgrade patch) write
`core/scheduler/default_timezone` (empty: the `TZ` env, then UTC) when they create the row; a later
change of that setting does not move an existing row. Each
value must pass the engine: two fires closer than `core/scheduler/min_interval_minutes` are
refused. A changed value takes effect at the next sync: the rows that use it get their next fire
computed again. The rows are read-only everywhere else: the panel shows them in the "Agent
schedules" group, and no user or tool can edit or delete them. Change the issue in Jira instead.

**Cron is no longer accepted (AG-67).** The upgrade to this release converts every stored
`frequency_map` value and every old schedule row from cron to RRULE (data patch
`ConvertFrequencyMapToRrule`). Before any change, `setup:upgrade` stops with
`upgrade refused, nothing was changed` and names each value that has no RRULE twin (`L`, `W`,
`#`, `?`, `@…`, names, or a restricted day-of-month together with a restricted day-of-week). Set
those values to an RRULE, or disable the named row, and upgrade again. An ENV override
`CONFIG__JIRA_PERIODIC_TASKS__FREQUENCY_MAP` is not converted: a cron value in it is skipped at
each sync with
`ERROR frequency_map entry '<label>' is not an RRULE; cron is no longer accepted (AG-67)`.
An old row with no agent_view is disabled (`last_error = no_agent_view`).

`publish jira-cron <KEY> [--agent-view <code>]` still publishes one periodic run by hand.

## Session continuity

Every job on one issue (`jira:<KEY>`) is one thread (see
[threads.md](../architecture/threads.md)): the turns of the issue run one at a
time, in order, and share the issue's thread. By default a turn resumes the session of the issue's
last finished turn, so the agent remembers the earlier rounds.

| Path | Type | Default | Scope | Jobs it governs |
|---|---|---|---|---|
| `jira/resume_session` | boolean | `1` | default, workspace, agent_view | `source = jira`, type `todo` or `followup` |
| `jira_periodic_tasks/resume_session` | boolean | `1` | default, workspace, agent_view | `source = jira`, type `cron` |

With the field `0` for the job's agent_view, the job still runs in the issue's thread and line (the
same order, the same shared directory), but it starts a fresh session: the claim writes no
`resume_job_id`, and the deprecated `resume_session_resolver` is not asked. A retry of a job
still resumes its own crashed attempt. The field is read again on every claim, so a change applies
to the jobs that are waiting. A follow-up that a periodic run schedules is a `followup` job of
source `jira`, so `jira/resume_session` decides for it, not `jira_periodic_tasks/resume_session`.

```bash
# Start every periodic run of one agent_view with a fresh session.
agento config:set jira_periodic_tasks/resume_session 0 --scope=agent_view --scope-id=<id>
```

The choice is made by a `job_claim_before` observer in each module (`src/session_policy.py`), which
sets `JobClaimBeforeEvent.fresh_session`. The read is strict: if a scope level cannot be read, the
observer raises and the claim defers the job, never answering with a less specific level's value.
With a module disabled, its observer is not registered and its jobs resume as by default.

## Attachments in the thread

`jira_get_issue` and `jira_get_attachment` save an attachment to the thread's `downloads/` dir:
`/workspace/artifacts/_threads/<key>/downloads/jira/<KEY>/<attachment id>_<file name>`. A file that
is already there (with its conversion, for a PDF or XLSX) is returned with no new download, so three
comments on one issue fetch each attachment once. The dir is written by the toolbox only; a run reads
it by the absolute path the tool returns and copies what it wants to change into `./shared`. A session
with no thread (`agento run`) saves to its own run dir under a generated name, with no reuse: the run
can change that dir, so the toolbox never writes a chosen name there. See
[DECISIONS.md](../../DECISIONS.md) D-THREAD-2.
