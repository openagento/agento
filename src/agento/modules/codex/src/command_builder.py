"""The single place the Codex CLI's flags are defined."""
from __future__ import annotations

from agento.framework.harness import HarnessRunContext, RunRequest

from .config import parse_toml_blob

_EXEC_FLAGS = ["--json", "--skip-git-repo-check"]
_BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
_RESUME_PROMPT = "Continue working from where you left off."


def _bypasses_sandbox(ctx: HarnessRunContext) -> bool:
    """Whether to keep today's hardcoded bypass flag.

    The flag overrides config.toml wholesale, so it must go the moment an operator
    expresses a sandbox intent in ``codex/config`` — otherwise their network block is
    silently dead. Anything else (no blob, or a blob we cannot parse) keeps today's
    behaviour; the unparseable case also fails the workspace build, so the run never
    reaches here with a blob the adapter rejected.
    """
    blob = ctx.harness_config.get("config")
    if not blob:
        return True
    try:
        return "sandbox_mode" not in parse_toml_blob(blob, "codex/config")
    except ValueError:
        return True


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
        if _bypasses_sandbox(ctx):
            cmd.append(_BYPASS_FLAG)
        model = req.model or ctx.model
        if model:
            cmd += ["--model", model]
        return cmd

    def interactive(self, ctx: HarnessRunContext, *, yolo: bool = False) -> list[str]:
        cmd = ["codex"]
        if yolo:
            cmd.append(_BYPASS_FLAG)
        if ctx.model:
            cmd += ["--model", ctx.model]
        return cmd

    def stdin_payload(self, ctx: HarnessRunContext, req: RunRequest) -> str | None:
        """No stdin: the prompt is argv-borne. Keeps stdin closed (DEVNULL)."""
        return None
