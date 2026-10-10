from __future__ import annotations

import json
import re
from pathlib import Path

from agento.framework.harness import SESSION_ID, RunResult, SubprocessRunner, move_session_into
from agento.modules.claude.src.output_parser import parse_claude_output


class ClaudeSubprocessRunner(SubprocessRunner):
    """Runs the Claude Code CLI. Commands come from ClaudeCommandBuilder."""

    def _parse_output(self, raw: str) -> RunResult:
        return parse_claude_output(raw, self.logger)

    def _credential_env(self, credential: object | None) -> dict[str, str]:
        if credential is None:
            return {}
        from agento.modules.claude.src.config import ClaudeWorkspaceAdapter
        return ClaudeWorkspaceAdapter().credential_env(credential)

    def _try_parse_session_id(self, line: str) -> str | None:
        try:
            event = json.loads(line.strip())
            if isinstance(event, dict) and event.get("session_id"):
                return event["session_id"]
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    def prepare_resume(self, session_id: str) -> bool:
        """Claude files a session under ``~/.claude/projects/<cwd, non-alphanumerics as ->/``
        and ``--resume`` looks only in the current cwd's folder."""
        if not SESSION_ID.fullmatch(session_id):
            return False
        if self.context.home_dir is None:
            return True  # the operator's own HOME: nothing of ours to move
        return move_session_into(
            Path(self.context.home_dir) / ".claude" / "projects",
            re.sub(r"[^A-Za-z0-9]", "-", self.context.working_dir),
            f"{session_id}.jsonl",
        )
