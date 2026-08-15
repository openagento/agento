"""Claude Code harness adapter — the single object the framework registers.

Static metadata (id, providers, capabilities, sandbox package) lives in ``di.json``;
this class supplies only behaviour.
"""
from __future__ import annotations

from collections.abc import Mapping

from agento.framework.harness import (
    CredentialAuthenticator,
    CredentialScope,
    HarnessRunContext,
)
from agento.modules.claude.src.auth import ClaudeCredentialAuthenticator
from agento.modules.claude.src.command_builder import ClaudeCommandBuilder
from agento.modules.claude.src.config import ClaudeWorkspaceAdapter
from agento.modules.claude.src.runner import ClaudeSubprocessRunner
from agento.modules.claude.src.stream_renderer import ClaudeStreamRenderer
from agento.modules.claude.src.transcript_reader import ClaudeTranscriptReader

CREDENTIAL_SCOPE = CredentialScope("claude")


class ClaudeHarnessAdapter:
    def __init__(self) -> None:
        self._command_builder = ClaudeCommandBuilder()
        self._workspace_adapter = ClaudeWorkspaceAdapter()
        self._transcript_reader = ClaudeTranscriptReader()
        self._stream_renderer = ClaudeStreamRenderer()
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
    def transcript_reader(self) -> ClaudeTranscriptReader:
        return self._transcript_reader

    @property
    def stream_renderer(self) -> ClaudeStreamRenderer:
        return self._stream_renderer

    @property
    def authenticators(self) -> Mapping[CredentialScope, CredentialAuthenticator]:
        return self._authenticators

    def create_runner(self, ctx: HarnessRunContext, **kwargs) -> ClaudeSubprocessRunner:
        return ClaudeSubprocessRunner(
            context=ctx, command_builder=self._command_builder, **kwargs
        )
