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
from agento.modules.claude.src.model_check import check_model
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

    def create_runner(self, ctx: HarnessRunContext, **kwargs) -> ClaudeSubprocessRunner:
        return ClaudeSubprocessRunner(
            context=ctx, command_builder=self._command_builder, **kwargs
        )
