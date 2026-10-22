# Jira channel

The `jira` module publishes work from Jira Cloud (assigned TODO issues, follow-ups on an issue) and
ships the `jira_*` toolbox tools. The `jira_periodic_tasks` module (it needs `jira`) publishes the
`cron` jobs of periodic issues. This page covers session continuity and attachments; the config fields are in
each module's `system.json`.

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
