# The `claude` harness

Runs Claude Code headless with `--output-format stream-json --verbose --include-partial-messages`.
The contract it implements is in [../architecture/harness-contract.md](../architecture/harness-contract.md).

## Model check

The Claude CLI has no model list. `agento config:test agent_view/model` accepts the aliases
`opus`, `sonnet`, `haiku` without a request, refuses an id that is not a valid Claude model id, and
otherwise asks `GET https://api.anthropic.com/v1/models/{id}` with the pool credential (no tokens
are spent): 200 is `MODEL_OK`, 404 is `MODEL_UNKNOWN`, any other answer is `MODEL_CHECK_FAILED`. The
request runs in a child process (`model_probe`) that gets the credential on stdin and prints only the
status code. Not yet verified: if this endpoint accepts a Claude subscription (OAuth) token; if it
does not, the check answers `MODEL_CHECK_FAILED`, never a false result.

## Live timeline fragments

`--include-partial-messages` adds `stream_event` lines. `stream_event_mapper` maps a
`content_block_delta` `text_delta` to `assistant.partial` and a `thinking_delta` to
`reasoning.partial`; the framework coalesces them (one row per 250 ms or 4 KiB). The complete
`assistant` message then gives, in content order, `assistant.reasoning` per thinking block,
`tool.started` per tool call and one `assistant.text` (its text blocks joined with `\n`). The
claude CLI sends thinking with empty text, so the panel shows "Thought" with no body.

## Final answer

The final answer is the text of the **last assistant message**: the `result` field of the
non-error `result` event. `job.output` holds only that text, and a channel posts it back.
The full event stream is not in `job.output`. It is in the conversation timeline and in the
native transcript on disk. When the stream has no usable `result` (a timeout, a killed
process), `job.output` keeps the raw stream so the operator can see what happened.

## Live timeline fragments

`stream_event_mapper` (`src/stream_event_mapper.py`) turns each stream-json event into
framework fragments:

| Event | Fragments |
|---|---|
| `assistant` | one `assistant.text` (the message's text blocks joined with `\n`), then one `tool.started` per `tool_use` block (`call_id` = block `id`, `input` = JSON of `input`) |
| `user` | one `tool.completed` per `tool_result` block (`call_id` = `tool_use_id`, `output` = string content or its text blocks, `is_error`) |
| `result` with `is_error` | `error` |

Thinking blocks, `system/init`, `rate_limit_event` and other events give no fragment.
