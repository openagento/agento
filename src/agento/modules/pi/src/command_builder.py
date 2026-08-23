from __future__ import annotations

from agento.framework.harness import HarnessRunContext, RunRequest

# The bridge extension, relative to the run directory (which is Pi's cwd).
BRIDGE_REL_PATH = ".pi/agento-toolbox.js"

# Handed to Pi on stdin when the consumer resumes a job. The consumer resumes with an
# EMPTY prompt, and Pi's print mode only prompts when the initial message is non-empty
# (`if (initialMessage)`, dist/modes/print-mode.js:103-105) — so an empty payload would
# open the session, print the header and exit rc=0 having done nothing, which the job
# would record as a success. Never return "" from stdin_payload for a resume.
RESUME_PROMPT = (
    "Continue the interrupted task from this session. Reconstruct the state from the "
    "session history and finish the work."
)


class PiCommandBuilder:
    """Every Pi CLI flag lives here — headless and interactive cannot drift.

    Notably absent, all deliberate:

    * ``--session`` / ``--continue`` / ``--resume``. A bare id landing in another cwd
      bucket makes Pi ask "Fork this session into current directory?" **on stdin**, which
      headless would answer with our prompt. ``--session-id`` looks up locally only and
      creates the session when absent, so it is the only safe resume flag — and it is
      mutually exclusive with the other three (hard ``exit 1``).
    * a prompt in argv. ``-p`` refuses a value starting with ``-`` (dist/cli/args.js:109-116),
      and Agento prompts come from Jira titles and mail subjects. The prompt goes on stdin.
    * a yolo/skip-approvals flag. Pi has no approval prompts by design, so there is nothing
      to bypass; ``yolo`` is accepted and ignored rather than mapped to something plausible.
    """

    def _shared_flags(self, ctx: HarnessRunContext) -> list[str]:
        """Flags common to both modes — the reason they cannot drift apart."""
        flags = ["--offline", "--no-extensions", "-e", BRIDGE_REL_PATH]
        if ctx.provider:
            flags += ["--provider", str(ctx.provider)]
        # A config value selects a FIXED flag; it is never interpolated into an argument.
        if ctx.harness_config.get("builtin_tools") == "0":
            flags.append("--no-builtin-tools")
        return flags

    def headless(self, ctx: HarnessRunContext, req: RunRequest) -> list[str]:
        model = req.model or ctx.model
        if not model:
            # Pi headless without a model exits 1; failing here names the cause.
            raise ValueError(
                "Pi requires a model. Set it with: agento config:set agent_view/model "
                "<exact-catalog-id> --scope=agent_view --scope-id=<n>"
            )
        cmd = ["pi", "--mode", "json", *self._shared_flags(ctx), "--model", model]
        if req.session_id:
            cmd += ["--session-id", req.session_id]
        return cmd

    def interactive(self, ctx: HarnessRunContext, *, yolo: bool = False) -> list[str]:
        cmd = ["pi", *self._shared_flags(ctx)]
        if ctx.model:
            cmd += ["--model", ctx.model]
        return cmd

    def stdin_payload(self, ctx: HarnessRunContext, req: RunRequest) -> str | None:
        """Pi's prompt channel. ``None`` only for the interactive path.

        A resume carries an empty ``req.prompt`` (see ``RESUME_PROMPT``), so it is
        substituted rather than passed through.
        """
        if req.prompt:
            return req.prompt
        if req.session_id:
            return RESUME_PROMPT
        return None
