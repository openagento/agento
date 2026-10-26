"""Claude Code harness adapter — the single object the framework registers.

Static metadata (id, providers, capabilities, sandbox package) lives in ``di.json``;
this class supplies only behaviour.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from agento.framework.harness import (
    SESSION_ID,
    CredentialAuthenticator,
    CredentialScope,
    HarnessRunContext,
)
from agento.modules.claude.src.auth import ClaudeCredentialAuthenticator
from agento.modules.claude.src.command_builder import ClaudeCommandBuilder
from agento.modules.claude.src.config import ClaudeWorkspaceAdapter
from agento.modules.claude.src.model_check import check_model
from agento.modules.claude.src.output_parser import parse_claude_output
from agento.modules.claude.src.runner import ClaudeSubprocessRunner
from agento.modules.claude.src.stream_event_mapper import ClaudeStreamEventMapper
from agento.modules.claude.src.stream_renderer import ClaudeStreamRenderer

CREDENTIAL_SCOPE = CredentialScope("claude")


class ClaudeHarnessAdapter:
    def __init__(self) -> None:
        self._command_builder = ClaudeCommandBuilder()
        self._workspace_adapter = ClaudeWorkspaceAdapter()
        self._stream_renderer = ClaudeStreamRenderer()
        self._stream_event_mapper = ClaudeStreamEventMapper()
        self._authenticators: dict[CredentialScope, CredentialAuthenticator] = {
            CREDENTIAL_SCOPE: ClaudeCredentialAuthenticator(),
        }

    @property
    def command_builder(self) -> ClaudeCommandBuilder:
        return self._command_builder

    @property
    def workspace_adapter(self) -> ClaudeWorkspaceAdapter:
        return self._workspace_adapter

    @property
    def stream_renderer(self) -> ClaudeStreamRenderer:
        return self._stream_renderer

    @property
    def stream_event_mapper(self) -> ClaudeStreamEventMapper:
        return self._stream_event_mapper

    @property
    def authenticators(self) -> Mapping[CredentialScope, CredentialAuthenticator]:
        return self._authenticators

    def check_model(self, provider, model, credential, *, timeout_s):
        return check_model(provider, model, credential, timeout_s=timeout_s)

    def stored_answer(self, output: str) -> str | None:
        """The answer in a ``job.output``. Before the parser kept only the answer, the
        output was the whole stream-json; its last ``result`` is the answer then."""
        if not output.startswith('{"type"'):
            return output
        try:
            answer = parse_claude_output(output).raw_output
        except Exception:
            return None
        return None if answer == output else answer

    def session_file(self, root: Path, session_id: str) -> Path | None:
        if not SESSION_ID.fullmatch(session_id):
            return None
        found = root.glob(f".claude/projects/*/{session_id}.jsonl")
        return max(found, key=lambda p: p.stat().st_mtime, default=None)

    def session_transcript(self, path: Path) -> list[list[dict]] | None:
        """The session's fragments, one list per turn, each with ``at`` = the line's timestamp.

        A turn starts at a prompt: a main-chain ``user`` line that is not meta and holds no
        ``tool_result``. Lines before the first prompt, side chains and live partials are dropped.
        """
        turns: list[list[dict]] = []
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict) or event.get("isSidechain") \
                            or event.get("type") == "stream_event":
                        continue
                    if event.get("type") == "user" and not event.get("isMeta") \
                            and not _has_tool_result(event):
                        turns.append([])
                    if not turns:
                        continue
                    mapped = self._stream_event_mapper.map_event(event) or []
                    for fragment in mapped if isinstance(mapped, list) else [mapped]:
                        if fragment["kind"].endswith(".partial"):
                            continue
                        if "timestamp" in event:
                            fragment["at"] = event["timestamp"]
                        turns[-1].append(fragment)
        except (OSError, ValueError):   # unreadable, or not UTF-8
            return None
        return turns

    def create_runner(self, ctx: HarnessRunContext, **kwargs) -> ClaudeSubprocessRunner:
        return ClaudeSubprocessRunner(
            context=ctx, command_builder=self._command_builder, **kwargs
        )


def _has_tool_result(event: dict) -> bool:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
