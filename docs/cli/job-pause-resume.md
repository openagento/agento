# job:pause / job:resume

Pause a running job and resume it later without losing the agent's conversation context.

## Usage

```bash
agento job:pause <job_id>    # Stop a running job, keep session
agento job:resume <job_id>   # Re-queue paused job for consumer pickup
```

## job:list — see recent / failed jobs

List jobs newest-first, optionally filtered — useful for spotting dead-lettered failures that otherwise
only surface via the admin TUI or a configured alert email.

```bash
agento job:list                            # 20 most recent jobs
agento job:list --status DEAD              # dead-lettered (retry-exhausted) failures
agento job:list --status FAILED            # jobs in the FAILED state
agento job:list --source outlook --limit 50
agento job:list --agent-view developer
```

Columns: `ID  STATUS  SOURCE  TYPE  AGENT_VIEW  REFERENCE  CREATED`; for `FAILED`/`DEAD` rows an indented
line shows `error_class: error_message`. `--status` accepts
`TODO`/`RUNNING`/`SUCCESS`/`FAILED`/`DEAD`/`PAUSED`. The command is read-only and queries in **strict**
mode, so a DB/query error is reported (non-zero exit) instead of being printed as "No jobs found".

> Terminal failures land in `DEAD` (the consumer retries up to `max_attempts`, then dead-letters). To be
> emailed on dead-letter, configure the `app_monitor` alerts (`alerts/smtp_host` + `alerts/email_to`);
> `job:list --status DEAD` is the pull-based equivalent.

## How pause works

1. The CLI sends SIGTERM to the agent subprocess (if the PID is alive).
2. The agent CLI (Claude, Codex) flushes its session transcript to disk on exit.
3. The job status is set to `PAUSED` in the database.

The session file remains on disk in the workspace volume (`/workspace/.claude/` or `/workspace/.codex/`), so it survives container restarts.

If the subprocess PID is already dead (e.g. the process crashed), only the status flip happens. The session file is already on disk.

## How resume works

1. The job status is set back to `TODO` and `pid` is cleared.
2. The consumer picks it up on its next poll cycle.
3. Because `session_id` is preserved and `attempt > 1`, the consumer's existing auto-resume path fires: it calls `runner.resume(session_id)` instead of `runner.run()`, which translates to `claude --resume <session_id>` or `codex resume <session_id>`.

No new mechanisms are introduced. Resume reuses the existing retry-resume infrastructure.

## Edge cases

| Scenario | Behavior |
|----------|----------|
| Pause a RUNNING job with live PID | SIGTERM, wait up to 3s, flip to PAUSED. |
| Pause a RUNNING job whose PID already died | Just flip status to PAUSED. Session file is on disk. |
| Pause a job in non-RUNNING state | Error: "Cannot pause job in status X". |
| Resume a PAUSED job | Flip to TODO. Consumer picks up and auto-resumes. |
| Resume a job without session_id | Error: session was not captured before pause. |
| Resume while the last run is `alive` or `unknown` (its runner does not answer) | Error: "its last run is …; retry later". The job stays PAUSED. |
| Resume a job in non-PAUSED state | Error: "Cannot resume job in status X". |

## Events

- `job_pause_after` with `JobPausedEvent(job=...)` -- dispatched after successful pause.
- `job_resume_after` with `JobResumedEvent(job=...)` -- dispatched after successful resume.

## Known limitation

Session storage is not scoped per agent_view. All sessions share `/workspace/.claude/` and `/workspace/.codex/`. Collisions are impossible (UUIDs), but if an operator inspects these directories by hand, transcripts from all agent_views appear side-by-side. Per-agent_view isolation can come later with the cron/sandbox container split.

## Stale job recovery

PAUSED jobs are not touched by the consumer's stale-job recovery pass. The recovery query only selects `status = 'RUNNING'` rows, so intentionally parked jobs are safe.
