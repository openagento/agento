"""The single place Claude Code's CLI flags are defined.

Previously split between ``TokenClaudeRunner._build_command`` and
``ClaudeCliInvoker.headless_command``, which had already drifted: the invoker omitted
``--mcp-config .mcp.json --strict-mcp-config``, so ``agento run <view> "<prompt>"``
started without the per-job MCP config the consumer path always injected.
"""
from __future__ import annotations

from agento.framework.harness import HarnessRunContext, RunRequest

# .mcp.json is resolved relative to the subprocess cwd (the per-job artifacts dir),
# and --strict-mcp-config stops the CLI from also loading a user-level MCP config.
_MCP_FLAGS = ["--mcp-config", ".mcp.json", "--strict-mcp-config"]
_RESUME_PROMPT = "Continue working from where you left off."


class ClaudeCommandBuilder:
    """Builds ``claude`` invocations for headless, resume and interactive modes."""

    def headless(self, ctx: HarnessRunContext, req: RunRequest) -> list[str]:
        if req.session_id:
            cmd = ["claude", "--resume", req.session_id, "-p", _RESUME_PROMPT]
        else:
            cmd = ["claude", "-p", req.prompt]
        cmd += [
            "--dangerously-skip-permissions",
            *_MCP_FLAGS,
            "--output-format", "stream-json",
            "--verbose",
        ]
        model = req.model or ctx.model
        if model:
            cmd += ["--model", model]
        return cmd

    def interactive(self, ctx: HarnessRunContext, *, yolo: bool = False) -> list[str]:
        cmd = ["claude", *_MCP_FLAGS]
        if yolo:
            cmd.append("--dangerously-skip-permissions")
        if ctx.model:
            cmd += ["--model", ctx.model]
        return cmd

    def stdin_payload(self, ctx: HarnessRunContext, req: RunRequest) -> str | None:
        """No stdin: the prompt is argv-borne. Keeps stdin closed (DEVNULL)."""
        return None
