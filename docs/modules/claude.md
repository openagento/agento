# The `claude` harness

[Claude Code](https://www.npmjs.com/package/@anthropic-ai/claude-code) as an Agento
harness, with one provider: **Anthropic** (interactive OAuth or API key).

At workspace-build time the adapter writes `.claude.json` (model, login state) and
`.claude/settings.json` (permissions and everything else Claude Code reads from its
settings file), plus `.mcp.json` for the Toolbox.

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
last `result` event (a background subagent makes the CLI print one per turn). `job.output` holds only that text, and a channel posts it back.
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

## Per-run setup

Each run gets its own run dir (HOME and cwd). Before the agent starts, Agento writes
`projects["<run dir>"].hasTrustDialogAccepted = true` into the run's `.claude.json`. The
interactive TUI (CLI 2.1.x) asks "trust this folder" once per cwd and its default answer
exits, so without it `agento run <code>` stopped at the dialog. Only that one run dir is
trusted — never the developer's `projects` map and never a parent path.

`agento run <code> --yolo` also passes `--settings '{"skipDangerousModePermissionPrompt":true}'`,
because 2.1.x asks to confirm bypass mode on every start (default answer: exit).

## `claude/trust_level`

`agent_view/claude/trust_level = full` writes `permissions.defaultMode =
"bypassPermissions"` and `skipDangerousModePermissionPrompt: true` into
`.claude/settings.json`. Any other value writes nothing. Before 2026-10-06 it wrote
`permissions.dangerouslySkipPermissions`, a key the CLI does not read, so the setting had
no effect. Headless jobs run with `--dangerously-skip-permissions` anyway; the setting
matters for an interactive run without `--yolo`.

## `claude/personality` (removed)

Agento used to copy `agent_view/claude/personality` into `.claude.json` as `systemPrompt`.
The CLI never reads that key (verified on 2.1.291), so the value never reached the model.
The write is removed. Put an agent_view's personality in `SOUL.md`
(`agent_view/instructions/soul_md`), which the agent does read.

## Telemetry

`job.toolbox_mcp_calls` counts toolbox tool calls from the session transcript, including
the transcripts of its subagents (`<session>/subagents/agent-*.jsonl`, CLI 2.1.x). A
background subagent makes the CLI print one `result` event per turn; turns, tokens and
duration are summed over them, and the cost is the largest `total_cost_usd` (the CLI
reports it cumulative). An error result whose text is only in `errors[]` keeps that text,
so error classification (auth, limit) still sees it.

## `claude/settings` — native settings.json passthrough

**Never put a credential in this field.** It is stored unencrypted in `core_config_data`
(a `runtime_config_fields` entry may not be `obscure`), it is readable with
`config:get` / `config:list`, and it is written into a file inside the agent container —
which by design holds no credential. Native keys that take a secret (`env`) have no
supported use here; MCP credentials belong in the Toolbox.

| | |
|---|---|
| Path | `claude/settings` |
| Format | raw Claude Code `settings.json` (JSON object) |
| Destination | `.claude/settings.json` in the build |
| Merge rule | deep-merged **over** the block Agento generates — the operator wins on a key collision |
| Invalid value | fails the workspace build (it is never silently skipped) |

```bash
# Block the built-in web tools for one agent_view. permissions.deny is honoured even
# under --dangerously-skip-permissions.
agento config:set claude/settings '{"permissions":{"deny":["WebSearch","WebFetch"]}}' \
  --scope=agent_view --scope-id=<id>

# Pick the model Claude's advisor tool uses.
agento config:set claude/settings '{"advisorModel":"fable"}' \
  --scope=agent_view --scope-id=<id>
```

The merge is nested, so a `permissions` block from the operator does not wipe the
`permissions.defaultMode` Agento derives from `claude/trust_level`.

In `agento admin` the field is on the **agent_view** node, beside the harness selector —
it is listed only when this view's harness is `claude` (see
[harness contract](../architecture/harness-contract.md#harness_option--showing-the-field-where-it-is-set)).

## `claude/permissions` (legacy)

`agent_view/claude/permissions` still works, but it now lands in
`.claude/settings.json` instead of `.claude.json` — Claude **ignores** a `permissions`
key in `.claude.json`, so the value used to have no effect. It is merged together with
`claude/settings`.

**Breaking change:** an **invalid** JSON value there used to be logged and skipped. It
now fails the workspace build. A silently dropped permissions block is a deny-list the
operator believes is in place and is not. Remedy:

```bash
agento config:get agent_view/claude/permissions --scope=agent_view --scope-id=<id>
# then fix the JSON, or unset the value
```

## See also

- [Harness contract](../architecture/harness-contract.md) — `runtime_config_fields` and the rest of the contract
- [The `codex` harness](codex.md), [the `pi` harness](pi.md)
