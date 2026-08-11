from __future__ import annotations

import json

from agento.framework.harness import RunResult, SubprocessRunner
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
