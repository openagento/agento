# The `codex` harness

Runs the OpenAI Codex CLI as `codex exec --json`. The contract it implements is in
[../architecture/harness-contract.md](../architecture/harness-contract.md).

## Final answer

The final answer is the text of the **last** `agent_message` item. Earlier `agent_message`
items are progress notes ("I will check X"). They show in the conversation timeline, but
they are not in `job.output`. A channel such as Jira therefore posts only the final message,
not every note of the run.

## Live timeline fragments

`stream_event_mapper` (`src/stream_event_mapper.py`) turns each NDJSON event into one
framework fragment. `item.started` and `item.completed` of one action share `item.id`,
which becomes `call_id`.

| Event | Fragment |
|---|---|
| `item.started` `command_execution` | `tool.started`, `tool_name` = `shell`, `input` = `command` |
| `item.started` `mcp_tool_call` | `tool.started`, `tool_name` = `tool`, `input` = JSON of `arguments` |
| `item.completed` `agent_message` | `assistant.text` |
| `item.completed` `reasoning` | `assistant.reasoning` (the builder passes `-c model_reasoning_summary=auto`, so the item carries a summary) |
| `item.completed` `command_execution` | `tool.completed`, `output` = `aggregated_output`, `is_error` when `exit_code` is not 0 |
| `item.completed` `mcp_tool_call` | `tool.completed`, `output` = text blocks of `result.content` (or the error message), `is_error` when `error` is set |
| `turn.failed`, `error` | `error` |

`codex exec --json` sends whole items only, so codex emits no `assistant.partial`: its text
appears when the message completes.
