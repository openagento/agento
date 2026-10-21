# Subagent module

Lets an agent that runs as a queued job give a task to an agent_view as a **child job**.
`agent_run` puts the child in the queue and returns at once. `agent_result` reads the child's
status and, after `SUCCESS`, its output. The normal consumer runs the child: there is no new
runtime.

- **Module:** `src/agento/modules/subagent/`
- **Depends on:** nothing (`sequence: []`). It reads the framework tables `job` and `agent_view`.
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
| `subagent/max_children_per_job` | 10 | Max child jobs that one job may create. Must be an integer >= 1. Any other value makes `agent_run` refuse with `agent_run is not configured`, never "no limit". |

The depth limit is **3** levels below the root job. It is a code constant, not config.

## `agent_run`

Arguments: `agent_view` (code, `^[A-Za-z0-9_-]{1,50}$`) and `prompt` (1–16000 chars). No other
argument is accepted: the parent, the depth and the caller's view come from the capability row.

The tool, in one transaction:

1. Takes the named lock `subagent:<parent job id>` (`GET_LOCK`, 10 s). This serializes the
   `agent_run` calls of one parent, so the child cap holds under concurrency. It is not a row
   lock: `SELECT … FOR UPDATE` needs UPDATE on `job`, and the toolbox DB user holds only
   `SELECT, INSERT` there (`framework/db_grants.py`).
2. Refuses at depth 3.
3. Finds the target view (active only). It must be the caller's own view or in
   `allowed_agent_views`.
4. Counts the parent's children against `max_children_per_job`.
5. Inserts the child job (`type = subagent`, `parent_job_id` = parent, `depth` = parent + 1,
   the parent's priority, `context` = the prompt) and a `job.queued` outbox row.

Result: `{"job_id": <id>, "status": "TODO", "depth": <d>}`.

The child gets only a short header and the prompt. Agento does not copy the parent's context.

## `agent_result`

Argument: `job_id`. Only the **direct parent** can read a child: the query binds the caller's
job id from the capability row (`WHERE id = ? AND parent_job_id = ?`). Result:

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
| `max <n> child jobs per job reached` | The parent already has `<n>` children |
| `job not found or not accessible` | The job does not exist, or it is not a direct child of the caller (sibling, grandchild, unrelated) |
| `agent_run failed` / `agent_result failed` | A DB error. The log has the error class; the caller gets no SQL |

## Limits

- A tree under one root holds at most `N + N² + N³` jobs (`N` = `max_children_per_job`).
- A delegated job (`depth > 0`) cannot call `schedule_followup`: it refuses with
  `schedule_followup is not available in a delegated job`. So every non-root job is an
  `agent_run` child, under the cap.
- No cancel, no wait, no dedupe of spawns. See ROADMAP.md.

## Disable

`module:disable subagent` removes both tools and the `subagent` job type. A child that is still
`TODO` then has a type that is not registered. The consumer does not select such a row (the
dequeue filters on registered types), so it does not stop the queue. The row waits with no
change and runs when you enable the module again.

## Security

Enabling `agent_run` for view A with `allowed_agent_views=B` is the operator's consent that A's
prompts drive B's tools. See [zero-trust](../architecture/zero-trust.md#known-exceptions-and-debt).
