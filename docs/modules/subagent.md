# Subagent module

Lets an agent that runs as a queued job give a task to an agent_view as a **child job**.
`agent_run` puts the child in the queue and returns at once. `agent_result` reads the child's
status and, after `SUCCESS`, its output. The normal consumer runs the child: there is no new
runtime.

- **Module:** `src/agento/modules/subagent/`
- **Depends on:** nothing (`sequence: []`). It reads the framework tables `job` and `agent_view`.
- **Observer:** `subagent_wake_parent` on `job_succeed_after`, `job_blocked_after` and
  `job_dead_after` (see [Parent wake](#parent-wake)).
- **Tools:** `agent_run`, `agent_result` (toolset `subagent`). Both are **opt-in**: they are off
  until you enable them.
- **Job type, channel, workflow:** `subagent`.

## Enable

```bash
agento tool:enable agent_run --agent-view <code>
agento tool:enable agent_result --agent-view <code>
```

Enable both tools for the view that delegates. The view that runs the child needs neither tool.

To let a view delegate to **other** views, set the allow-list at the caller's scope:

```bash
agento config:set subagent/allowed_agent_views "reviewer,writer" --agent-view <code>
```

## Config

| Path | Default | Meaning |
| --- | --- | --- |
| `subagent/allowed_agent_views` | empty | Comma-separated agent_view codes this scope may delegate to. Empty = own view only. There is no `*` value. |
| `subagent/max_children_per_job` | 10 | Max sub-agents one agent may start: per job at depth 0, per agent line deeper (see [Lines and scope](#lines-and-scope)). Must be an integer >= 1. Any other value makes `agent_run` refuse with `agent_run is not configured`, never "no limit". |
| `subagent/wake_parent` | 1 | Resume the parent when all its children finish. Read at the parent's view. Also changes the `agent_run` description: on, it tells the agent to end its turn and not poll; off, to poll `agent_result` or use `schedule_followup` (root jobs only). |

The depth limit is **3** levels below the root job. It is a code constant, not config.

## Lines and scope

A child runs in its parent's **thread** (so in the parent's conversation, whatever its view) and
starts a **line**: the sequence of turns of one logical agent. At depth 0 the line is every
depth-0 job of the thread (each Jira comment, each wake). At depth > 0 the line is keyed by its
first job's id: a continuation (`agent_run` with `job_id`) and a wake are new jobs of that line
(`line_id` = the first job's id), and the claim resumes the line's latest session
(`framework/threads.py`).

**Scope rule.** A caller may address job X (`agent_result`, `agent_run` with `job_id`) when X
was delegated from a job of the caller's line, in the caller's thread. So a later turn of the
line (a wake, the next Jira comment) reaches the children an earlier turn started; a sibling
branch, a grandchild and a job in another thread are out of scope. X is a child's first job, a
continuation or a wake of a child's line. The caller's job comes from the capability row, never
from an argument.

## `agent_run`

Arguments: `agent_view` (code, `^[A-Za-z0-9_-]{1,50}$`), `prompt` (1–16000 chars) and,
optionally, `job_id`: an earlier child whose session continues with this prompt. No other
argument is accepted: the parent, the depth, the thread and the caller's view come from the
capability row.

The tool, in one transaction:

1. Takes the named lock `subagent:<parent job id>` (`GET_LOCK`, 10 s). This serializes the
   `agent_run` calls of one parent, so the child cap holds under concurrency. It is not a row
   lock: `SELECT … FOR UPDATE` needs UPDATE on `job`, and the toolbox DB user holds only
   `SELECT, INSERT` there (`framework/db_grants.py`).
2. Refuses at depth 3.
3. Finds the target view (active only). It must be the caller's own view or in
   `allowed_agent_views`.
4. With `job_id` = X: X must be in scope, X's view must be `agent_view`, and no job of X's
   line may be open (TODO, RUNNING, PAUSED).
5. Counts against `max_children_per_job`: at depth 0 the `subagent` jobs of this job; at
   depth > 0 the `subagent` jobs of the whole caller line (this bounds the wakes under one
   parent). Wake jobs are `followup` and do not count. The read stops at the cap.
6. Inserts the child job (`type = subagent`, `parent_job_id` = caller, `depth` = caller + 1,
   the caller's thread, `line_id` = X's line or NULL, the caller's priority, `context` = the
   prompt) and a `job.queued` outbox row.

Result: `{"job_id": <id>, "status": "TODO", "depth": <d>}`.

The child gets only a short header and the prompt. Agento does not copy the parent's context.

## `agent_result`

Argument: `job_id`. The [scope rule](#lines-and-scope) decides which jobs the caller reads; the
query binds the caller's job id from the capability row. Result:

- always `job_id` and `status` (the job's own status);
- `SUCCESS` adds `output` (the harness's final answer; a large one is offloaded to an artifact
  file, as for any tool result);
- `FAILED` or `DEAD` adds `error`: the coarse `error_class` only. `error_message` can quote CLI
  output and stays admin-only.

## Errors

Every refusal inserts nothing. The texts are exact:

| Text | When |
| --- | --- |
| `agent_run requires an Agento job context` | No job on the capability (for example `mcp_interactive`, `user_session`, `miniapp`), or the job is not the capability's view |
| `agent_result requires an Agento job context` | Same, for `agent_result` |
| `sorry, max 3 nested subagents levels` | The parent is at depth 3 |
| `agent_view not found or not allowed` | The target is unknown, inactive, or not allowed. One text for all three, so it tells nothing about which views exist |
| `agent_run is not configured` | `max_children_per_job` is not an integer >= 1 (a WARN log names the scope) |
| `max <n> sub-agents for this agent` | The caller (at depth 0) or its line (deeper) already started `<n>` sub-agents |
| `job not found or not accessible` | The job does not exist, or it is out of scope (sibling branch, grandchild, another thread), or `job_id` names a child of another view than `agent_view` |
| `job is still running` | `job_id` names a child whose line has an open job |
| `agent_run failed` / `agent_result failed` | A DB error. The log has the error class; the caller gets no SQL |

## Parent wake

With `wake_parent` on, a parent does not poll. When a job R ends (SUCCESS, blocked or DEAD -
also from stale recovery; a retry is not an end), the observer checks R and each job above it,
nearest first. Every ancestor, not only the parent: a child that is never woken itself (DEAD,
blocked, `wake_parent` off in its view) may end before its own children, and then the last
grandchild's end is the only event left for the jobs above.
A job P is woken once - one `followup` job with the idempotency key `subagent_wake:<P>` - when:

- P is SUCCESS, has a view and a `reference_id`, `wake_parent` is on for P's view, and P has a
  child;
- no job under P (three levels) is open;
- no job under P is itself due for a wake (it meets the first rule and has no wake job yet).

The wake job is a new turn of P's line: P's source, reference, view, priority and thread;
`parent_job_id` and `depth` of P; at depth > 0 `line_id` = P's line. The claim resumes P's
session. Its prompt lists P's children, one line per child line (its latest turn):

```text
Sub-agents you started have finished:
#101 (reviewer) SUCCESS
#102 (tester) DEAD
Read each result with agent_result.
```

The last rule makes the wake race-free: a child whose own children just finished is due until
its wake job exists, and that job is open until it ends, so the parent never wakes while a turn
below it is due. Two observers that decide the same wake publish one job (the key). The reads
are batched: per job checked (at most four), one query per level and one for the wake keys; per
end, one read per level above the parent and one config read per distinct view.
Five parallel children give one wake.

A wake lost to a consumer crash between the commit and the observer is not published again
(see ROADMAP.md).

## Limits

A tree has two kinds of jobs besides its root (`N` = `max_children_per_job`):

- **`subagent` jobs** (from `agent_run`) are capped. Each line delegates at most `N`, and
  depth stops at 3, so a tree holds at most `N + N² + N³` of them.
- **`followup` wake jobs** (key `subagent_wake:<P>`) are not under the cap. Each one wakes a
  job that delegated, so there is at most one per `subagent` job: `N` at depth 0 (turns of the
  root's line), `N²` at depth 1 and `N³` at depth 2.

So a tree holds at most `2(N + N² + N³)` jobs besides its root, and `N + 2N² + 2N³` of them at
`depth > 0` (with `N = 1`: 3 `subagent` jobs and 2 wake jobs).

- Under one parent job there are at most `N` `subagent` jobs and `N²` wake jobs: at most `N`
  child lines, and each wake of a line needs one delegation counted against that line's cap.
- A delegated job (`depth > 0`) cannot call `schedule_followup`: it refuses with
  `schedule_followup is not available in a delegated job`. So a job at `depth > 0` is either an
  `agent_run` child (under the cap) or a wake (one per job that delegated).
- No cancel, no wait, no dedupe of spawns. See ROADMAP.md.

## Disable

`module:disable subagent` removes both tools and the `subagent` job type. A child that is still
`TODO` then has a type that is not registered. The consumer does not select such a row (the
dequeue filters on registered types), so it does not stop the queue. The row waits with no
change and runs when you enable the module again.

## Security

Enabling `agent_run` for view A with `allowed_agent_views=B` is the operator's consent that A's
prompts drive B's tools. See [zero-trust](../architecture/zero-trust.md#known-exceptions-and-debt).
