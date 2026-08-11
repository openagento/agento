"""The single place the Codex CLI's flags are defined."""
from __future__ import annotations

from agento.framework.harness import HarnessRunContext, RunRequest

_EXEC_FLAGS = [
    "--json",
    "--dangerously-bypass-approvals-and-sandbox",
    "--skip-git-repo-check",
]
_RESUME_PROMPT = "Continue working from where you left off."


class CodexCommandBuilder:
    """Builds ``codex`` invocations for headless, resume and interactive modes."""

    def headless(self, ctx: HarnessRunContext, req: RunRequest) -> list[str]:
        if req.session_id:
            # Non-interactive resume is `codex exec resume <id> <prompt>` —
            # `codex resume` needs a TTY.
            cmd = ["codex", "exec", "resume", req.session_id, _RESUME_PROMPT]
        else:
            cmd = ["codex", "exec", req.prompt]
        cmd += _EXEC_FLAGS
        model = req.model or ctx.model
        if model:
            cmd += ["--model", model]
        return cmd

    def interactive(self, ctx: HarnessRunContext, *, yolo: bool = False) -> list[str]:
        cmd = ["codex"]
        if yolo:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        if ctx.model:
            cmd += ["--model", ctx.model]
        return cmd
