# Threads, lines and the thread directory

A **thread** is the jobs of one conversation of work: the comments on one Jira issue, one mail
conversation, one pull request, one panel conversation. The jobs of one line run one at a time and
continue one harness session; all jobs of a thread share one directory. Code:
`framework/threads.py`, `framework/artifacts_dir.py`, migration `054_thread.sql`. Decisions:
[DECISIONS.md](../../DECISIONS.md) D-THREAD-1 (channel thread), D-THREAD-2 (directory), D-THREAD-3
(resume), D-THREAD-4 (Outlook gate), D-SUBAGENT-LINE-1, D-FRESH-SESSION-1.

## Identity

A thread row is `(agent_view_id, sha1(ref))`, unique. `ref` is what the channel names; with none,
`default_ref()` is the job's source item (`<source>:<reference_id>`) or the job itself
(`job:<id>`). The row also holds a random `key` (32 hex characters, 128 bits): the name of the
thread's directory. A thread with no view (its view was deleted) belongs to no one: a depth-0 job
cannot join it.

| Channel | Thread ref |
|---|---|
| Jira (todo, mention, periodic task, follow-up) | `jira:<KEY>` (the default) |
| Outlook | `outlook:conversation:<conversationId>` only while `outlook_list_thread` is effective for the view; else one thread per message ([outlook.md](../modules/outlook.md#reads-and-thread-actions-are-bound-to-the-triggering-message)) |
| GitHub, Bitbucket | `github:<ref>`, `bitbucket:<ref>`: both lanes of one PR |
| Panel conversation | `conversation:<id>`: the conversation is 1:1 with its thread ([conversations.md](conversations.md)) |
| `schedule_followup`, a wake | the thread of the job that made it (`thread_id` is copied) |
| `agent_run` | the parent's thread, also for a child in another view |

## Insert time

Every job gets its thread when it is inserted (`publisher.insert_job`, and the Node inserts of
`schedule_followup` and `agent_run`). `insert_job` locks the thread row until the caller commits,
so the jobs of one thread commit in id order. A depth-0 job may join only a thread of its own
view (SEC-7). A row inserted by other code with no thread gets its default thread at claim
(`place_claim`), so no job runs without one.

## Lines

A **line** is the turns of one logical agent in a thread. All depth-0 jobs of a thread are one
line. A sub-agent job (`depth > 0`) starts its own line (the key is its own id) or continues one
(`line_id`, written once at insert, when `agent_run` gets a `job_id`). A line never crosses a
thread.

**One open turn per line.** At claim, a job waits while an earlier turn of its line is open
(`line_blocked`): a started turn goes first, then the lower id. The claim defers the job, 1 s
doubled up to 5 s: it opens or extends a defer stretch with `line_wait = 1`, and one
`job_defer_after` fires when the stretch ends. So the comments on one issue run in
order, one at a time, and two lines of one thread (a parent and its children) run in parallel.
The claim holds the job row, then the thread row (`lock_thread`): two consumers never start two
turns of one line.

## Resume

At claim the job's `resume_job_id` is the turn of its line that **finished last** and has a
session (`resolve_resume`). The run loads that session when it ran on the same harness; else it
starts a fresh session (a panel conversation then sends the whole thread from the database). One mechanism serves every channel: a Jira comment, a mail reply,
a follow-up, a wake and a continued sub-agent all resume the end of their line, so a session never
forks. The claim writes `resume_job_id` on the row, so a FAILED or DEAD job records what it used.

- **A retry** of a job that saved a session resumes its own crashed attempt.
- **A fresh session** (`JobClaimBeforeEvent.fresh_session`, set by `jira/resume_session = 0` or
  `jira_periodic_tasks/resume_session = 0`): the job stays in its thread and line, with no resume
  target.
- **A detach** (`JobClaimBeforeEvent.detach_thread`, set by the Outlook gate when thread read is
  revoked): the claim moves the job to its default thread and clears its session before the line
  rules run.
- The deprecated `resume_session_resolver` seam is asked only when the job has no target and was
  neither detached nor made fresh (removal: ROADMAP.md).

## The thread directory

```
/workspace/artifacts/_threads/            0711 agent            (no run can list the keys)
└── <key>/                                2750 agent:agento_thread
    ├── shared/                           2770 agent:agento_thread   every run of the thread
    └── downloads/                        2750 agent:agento_thread   the toolbox writes, runs read
        └── jira/<KEY>/<id>_<name>        files 0640
<run dir>/shared -> /workspace/artifacts/_threads/<key>/shared
```

cron makes the dirs at every claim (`prepare_thread_dir`: a descriptor walk with `O_NOFOLLOW` and
an owner check, so a planted symlink fails the run) and links `./shared` in the run dir. Every run
uid gets the group `THREAD_GID` 19990 (`runner/uids.py`; the sandbox image adds `agent` to
`agento_thread`), so the runs of a thread, each with its own uid, edit one another's files in
`shared/`. An image built before this change has no group: a threaded job then fails closed with
"not in the thread group" until the image is rebuilt. `agento run` has no job, so it gets no
link.

`downloads/` is the toolbox's: a tool that fetches a file for the thread (Jira attachments) writes
it there, under a name it chooses, through dirs no run can write (SEC-5), and skips a file that is
already there. So three comments on one issue download each attachment once. A run reads a
download by the absolute path the tool returns, and copies it into `./shared` to change it
([file-manager.md](file-manager.md)).

## Trust model

The agents of one thread trust one another: they share a session and a directory. The framework
gives a run only its own thread's link, but the boundary is the random key plus the one group: a
run that knows another thread's absolute path (for example from a prompt an earlier turn wrote)
can open it. A sub-agent in another view joins its parent's thread and so uses its directory; the
operator's `subagent/allowed_agent_views` entry is the consent. These are listed in
[zero-trust.md](zero-trust.md#known-exceptions-and-debt).

## Sub-agents and the wake

A child job joins its parent's thread at `depth + 1` and starts its own line. A caller reads and
continues the children of its own line (`agent_result`, `agent_run(job_id=…)`). With
`subagent/wake_parent` on, the parent ends its turn instead of polling; when its last child ends,
an observer publishes a `followup` wake job into the parent's line, which resumes the parent's
session ([subagent.md](../modules/subagent.md#parent-wake)). The panel shows a child as a card
in its parent's turn ([conversation.md](../modules/conversation.md#turns-and-sub-agents-in-the-panel)).

## Settings

`jira/resume_session`, `jira_periodic_tasks/resume_session` and `subagent/wake_parent`
([config](../config/README.md#thread-and-session-settings)).

## Left out

`_threads/` retention, a "Shared files" panel tab and a sweep for a missed wake: ROADMAP.md.
