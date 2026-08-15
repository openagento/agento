"""Codex harness adapter — the single object the framework registers.

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
from agento.modules.codex.src.auth import CodexCredentialAuthenticator
from agento.modules.codex.src.command_builder import CodexCommandBuilder
from agento.modules.codex.src.config import CodexWorkspaceAdapter
from agento.modules.codex.src.runner import CodexSubprocessRunner
from agento.modules.codex.src.stream_renderer import CodexStreamRenderer
from agento.modules.codex.src.transcript_reader import CodexTranscriptReader

CREDENTIAL_SCOPE = CredentialScope("codex")


class CodexHarnessAdapter:
    def __init__(self) -> None:
        self._command_builder = CodexCommandBuilder()
        self._workspace_adapter = CodexWorkspaceAdapter()
        self._transcript_reader = CodexTranscriptReader()
        self._stream_renderer = CodexStreamRenderer()
        self._authenticators: dict[CredentialScope, CredentialAuthenticator] = {
            CREDENTIAL_SCOPE: CodexCredentialAuthenticator(),
        }

    @property
    def command_builder(self) -> CodexCommandBuilder:
        return self._command_builder

    @property
    def workspace_adapter(self) -> CodexWorkspaceAdapter:
        return self._workspace_adapter

    @property
    def transcript_reader(self) -> CodexTranscriptReader:
        return self._transcript_reader

    @property
    def stream_renderer(self) -> CodexStreamRenderer:
        return self._stream_renderer

    @property
    def authenticators(self) -> Mapping[CredentialScope, CredentialAuthenticator]:
        return self._authenticators

    def create_runner(self, ctx: HarnessRunContext, **kwargs) -> CodexSubprocessRunner:
        return CodexSubprocessRunner(
            context=ctx, command_builder=self._command_builder, **kwargs
        )
