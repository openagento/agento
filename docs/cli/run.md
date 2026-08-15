# `agento run` — Run the configured agent CLI

Spawns the agent CLI inside the `sandbox` container, with `HOME` and the working directory set to a per-run artifacts directory copied from the agent_view's current workspace build. Credentials, SSH key, instructions, and skills are all resolved naturally from that HOME. The exact CLI command is built by the harness's registered `CommandBuilder` — no harness-specific logic lives in the `run` command itself.

Two modes are selected automatically by presence of a prompt argument:

| Invocation | Mode | Semantics |
|---|---|---|
| `agento run <code>` | **Interactive** | Opens a TTY session inside the sandbox (`docker exec -it`). Signals, paste, arrow keys work as if the CLI were local. The agent's own per-action approval prompts apply — unless you pass `--yolo`. |
| `agento run <code> "<prompt>"` | **Headless (one-shot)** | Runs the agent with the given prompt, streams output to your terminal, then exits with the agent's exit code (`docker exec -T`). Stdin is closed. Add `--pretty` for a readable stream. |

Shortcut: `ru`.

### `--yolo` — skip interactive approval prompts

By default an interactive session uses the agent CLI's normal in-session approval prompting. Pass `--yolo` to run in the same **bypass mode** headless jobs always use — Claude with `--dangerously-skip-permissions`, Codex with `--dangerously-bypass-approvals-and-sandbox` — so the session never stops to ask for per-action approval:

```bash
agento run dev_01 --yolo        # interactive, no approval prompts
agento run --yolo dev_01        # same — flag may precede the code
```

This is safe by construction: the agent runs inside the isolated `sandbox` container with no credentials of its own (the toolbox is the only container with secrets). `--yolo` only affects **interactive** mode — headless (one-shot) runs are always in bypass mode, so the flag is a no-op there.

### `--pretty` — human-readable event stream

A headless run streams the agent CLI's own machine format — Claude Code `stream-json` JSONL, Codex `--json` NDJSON. That is right for parsing and unreadable for a person watching the run. `--pretty` renders each event as one line instead:

```bash
agento run qa_01 --pretty "create a PR for this branch"
agento run --pretty qa_01 "…"    # same — flag may precede the code
```

```
· claude-opus-4-6 · session 1f48657e · 42 tools
I will read the branch first.
⏺ Bash(git log --oneline -5)
  ⎿  c928a7f [feat] New channel: GitHub (+4 lines)
✓ done · 4 turns · 31.2s · $0.1140
```

Notes:
- Applies to **headless** runs only. Interactive sessions already draw their own output, so the flag prints a note and is ignored there.
- Raw JSONL stays the default — nothing about the machine-readable path changes.
- **It never loses output.** A line that is not JSON, a renderer that raises, or a harness with no renderer all fall back to printing the raw line, so `--pretty` can degrade but never swallow or crash a run.
- Colour is used only when stdout is a TTY, so `agento run … --pretty > log.txt` yields clean text.
- A result is shown as its **first line**; the rest is counted, not hidden — `… (+4 lines)`. Drop `--pretty` for the whole text.

Rendering is per harness, because each harness's stream format is its own: the framework asks the harness for a `StreamRenderer` rather than parsing the events itself. A harness that ships none simply has no pretty mode. See [harness contract](../architecture/harness-contract.md).

## Usage

### Interactive

```bash
agento run dev_01       # Opens the configured agent CLI for agent_view 'dev_01'
agento run qa_01        # Same, for a different agent_view
```

### Headless (one-shot)

```bash
agento run dev_01 "what MCP tools and skills do you have?"
agento run dev_01 "refactor src/foo.py to use dataclasses"
```

Everything after the agent_view code is treated as a single prompt string (via `argparse.REMAINDER`). Multi-word prompts do not need extra quoting beyond the shell's usual rules.

**Example headless session:**

```
$ agento run dev_01 "jakie masz toole z mcp i skille?"
OpenAI Codex v0.121.0 (research preview)
--------
workdir: /workspace/artifacts/it/dev_01/run-1234-a1b2c3d4e5f6
model: gpt-5.4
provider: openai
approval: never
sandbox: danger-full-access
session id: 019dab1e-73c3-7392-86c2-67ccd693a161
--------
user
jakie masz toole z mcp i skille?
codex
Mam w tej sesji dostęp do tych grup narzędzi MCP i skilli.
...
```

Exit code of the agent CLI is propagated to the shell, so headless mode composes naturally with scripts, CI, and `make`.

## What It Does

1. Calls `docker compose exec -T cron agento agent_view:prepare-run <code>` to run the **same pre-spawn pipeline the consumer runs for a real job**: claims a credential from the LRU pool for the view's own scope (stamping `used_at`; a provider that needs none simply gets no credential), materializes a unique per-run artifacts directory under `workspace/artifacts/<workspace>/<agent_view>/<run_id>/`, writes that credential into the artifacts HOME via the harness's `WorkspaceAdapter`, and asks the harness's `CommandBuilder` for the unified CLI **command** plus any **env-delivered credentials**. When a prompt is provided, the host passes `--prompt <prompt>` so cron returns the **headless** command instead of the interactive one; `--yolo` is forwarded the same way so the builder produces the interactive command in bypass mode. The host code itself stays agent-agnostic.
2. Validates that a build exists on the host at `workspace/build/<workspace>/<agent_view>/current/`.
3. Executes the returned command inside `sandbox` with `HOME` and `-w` (cwd) both set to the per-run artifacts dir. Any API-key values from the `env` field are injected via docker's **name-only** `-e KEY` form so the secret never appears in `ps`/argv — the value is read from the parent process's environment:
   - **Interactive:** `os.environ.update(env); os.execvp("docker", [..., "exec", "-it", "-u", "agent", "-e", "HOME=…", "-e", "TERM=…", *[("-e", k) for k in env], "-w", <working_dir>, "sandbox", *command])` — replaces the current process so the TTY transfer is clean.
   - **Headless:** `subprocess.run([..., "exec", "-T", "-u", "agent", "-e", "HOME=…", *[("-e", k) for k in env], "-w", <working_dir>, "sandbox", *command], env={**os.environ, **env}, stdin=subprocess.DEVNULL)` — waits for completion and propagates the exit code. With `--pretty` the same argv runs under `subprocess.Popen(..., stdout=subprocess.PIPE)` so the host can render each event line; `stdin`, env and the exit code are identical, and `stderr` stays inherited.

## Agent-Agnostic Architecture

`agento run` never branches on a harness name. Support for a new agent (OpenCode, Hermes, …)
requires **zero edits to the framework** — the agent module declares one harness in its
`di.json` and ships an adapter whose `CommandBuilder` owns its flags:

```json
{
  "agent_harnesses": [
    {
      "id": "myagent",
      "label": "My Agent",
      "class": "src.adapter.MyAgentHarnessAdapter",
      "default_provider": "myvendor",
      "providers": [
        {"id": "myvendor", "label": "My Vendor", "credential_required": true,
         "registration_modes": ["api_key"], "credential_scope": "myagent"}
      ]
    }
  ]
}
```

```python
class MyAgentCommandBuilder:
    def headless(self, ctx: HarnessRunContext, req: RunRequest) -> list[str]:
        cmd = ["myagent", "run", "--prompt", req.prompt]
        if req.model or ctx.model:
            cmd += ["--model", req.model or ctx.model]
        return cmd

    def interactive(self, ctx: HarnessRunContext, *, yolo: bool = False) -> list[str]:
        cmd = ["myagent"]
        if yolo:                       # `agento run <code> --yolo`
            cmd.append("--skip-approvals")
        return cmd
```

Both modes come from the **same** object on purpose. Previously Claude's flags lived in
two places — the consumer's runner and the `CliInvoker` that `agento run` used — and had
drifted: **neither** of the invoker's modes carried
`--mcp-config .mcp.json --strict-mcp-config`, which the runner always injected. So every
`agento run`, interactive or headless, started the agent without the per-job toolbox
config while real jobs got it. Protocols live in
[`src/agento/framework/harness/protocols.py`](../../src/agento/framework/harness/protocols.py);
shipped implementations: [`claude/src/command_builder.py`](../../src/agento/modules/claude/src/command_builder.py),
[`codex/src/command_builder.py`](../../src/agento/modules/codex/src/command_builder.py).
Full contract: [../architecture/harness-contract.md](../architecture/harness-contract.md).

## Preconditions

- Containers running: `agento up` (or `cd docker && docker compose -f docker-compose.dev.yml up -d`).
- `agent_view/harness` configured (the provider defaults to that harness's own
  `default_provider`):
  ```bash
  agento config:set agent_view/harness claude --agent-view dev_01
  ```
- Workspace build exists (config, SSH key, instructions, and assets materialized):
  ```bash
  agento workspace:build --agent-view dev_01
  ```

## Errors

| Message | Fix |
|---|---|
| `agent_view 'xyz' not found` | Check `agento config:list agent_view` and the `agent_view` table. |
| `no harness configured` | `agento config:set agent_view/harness <harness> --agent-view <code>` |
| `harness 'X' is not registered` | The agent module for `X` must declare an `agent_harnesses` entry in `di.json`. Built-in harnesses: `claude`, `codex`. |
| `no build found` | `agento workspace:build --agent-view <code>` |
| docker exec error | Start containers: `agento up`. |

## Inspection

Two cron-side commands let you inspect what `agento run` would resolve, without spawning the sandbox.

### `agent_view:runtime` — read-only debug (no side effects)

Resolves provider/model/home but does **not** touch the token pool or materialize an artifacts directory. Safe to call freely.

```bash
agento agent_view:runtime dev_01
# → {"agent_view_id": 2, "agent_view_code": "dev_01",
#    "workspace_id": 1, "workspace_code": "it",
#    "harness": "claude", "provider": "anthropic", "model": "claude-opus-4-6",
#    "home": "/workspace/build/it/dev_01/current",
#    "interactive_command": ["claude"],
#    "headless_command": null}

# Add --yolo to see the bypass-mode interactive command:
agento agent_view:runtime dev_01 --yolo
# → {..., "interactive_command": ["claude", "--dangerously-skip-permissions"]}

# Ask for the headless command too by passing a prompt:
agento agent_view:runtime dev_01 --prompt "hello"
# → {..., "headless_command": ["claude", "-p", "hello",
#      "--dangerously-skip-permissions", "--output-format", "stream-json",
#      "--verbose", "--model", "claude-opus-4-6"]}

# Override the model ad-hoc (doesn't persist to DB):
agento agent_view:runtime dev_01 --prompt "hello" --model claude-sonnet-4-6
```

### `agent_view:prepare-run` — what `agento run` actually invokes

This is the command the host calls under the hood. It resolves a token (stamps `used_at` in the LRU pool!), materializes the per-run artifacts directory on `/workspace`, and returns the unified `command`, the harness's `stream_renderer` path (for `--pretty`; `null` when the harness ships none), plus an `env` dict for API-key credential delivery. **Calling it has side effects** (token rotation + artifacts dir creation) — use sparingly when introspecting.

```bash
agento agent_view:prepare-run dev_01 --prompt "hello"
# → {"agent_view_id": 2, "agent_view_code": "dev_01",
#    "workspace_id": 1, "workspace_code": "it",
#    "harness": "claude", "provider": "anthropic", "model": "claude-opus-4-6",
#    "home": "/workspace/artifacts/it/dev_01/run-1234-a1b2c3d4e5f6",
#    "working_dir": "/workspace/artifacts/it/dev_01/run-1234-a1b2c3d4e5f6",
#    "command": ["claude", "-p", "hello", "--dangerously-skip-permissions",
#                "--mcp-config", ".mcp.json", "--strict-mcp-config",
#                "--output-format", "stream-json", "--verbose",
#                "--model", "claude-opus-4-6"],
#    "stream_renderer": "agento.modules.claude.src.stream_renderer:ClaudeStreamRenderer",
#    "env": {"ANTHROPIC_API_KEY": "sk-ant-…"},
#    "credential_id": 42, "token_id": 42}
```

Note: the secret value **only** appears in the JSON payload over the docker exec pipe; the host never echoes it back, and on cron-side parse failures stdout is suppressed (never re-printed to stderr) to avoid leaking the `env` field through error messages.

## Related

- [workspace-build.md](workspace-build.md) — how the build directory is materialized
- [../config/identity.md](../config/identity.md) — how SSH keys and credentials are stored per agent_view
- [../architecture/harness-contract.md](../architecture/harness-contract.md) — the harness/provider/model split and the full plugin contract
- [../architecture/events.md](../architecture/events.md) — framework extensibility model
