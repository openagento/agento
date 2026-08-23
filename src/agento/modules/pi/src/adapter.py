"""Pi harness adapter — the single object the framework registers.

Static metadata (id, providers, capabilities, sandbox package, runtime_config_fields)
lives in ``di.json``; this class supplies only behaviour.
"""
from __future__ import annotations

from collections.abc import Mapping

from agento.framework.harness import (
    CredentialAuthenticator,
    CredentialScope,
    HarnessRunContext,
)

from .auth import PiOpenRouterAuthenticator
from .command_builder import PiCommandBuilder
from .config import PiWorkspaceAdapter
from .runner import PiSubprocessRunner
from .transcript_reader import PiTranscriptReader

# One scope per credential-requiring provider. `ollama` requires none, so it has no
# entry here — the registry checks this mapping's keys against exactly the
# credential-requiring scopes the descriptor declares.
CREDENTIAL_SCOPE = CredentialScope("openrouter")


class PiHarnessAdapter:
    def __init__(self) -> None:
        self._command_builder = PiCommandBuilder()
        self._workspace_adapter = PiWorkspaceAdapter()
        self._transcript_reader = PiTranscriptReader()
        self._authenticators: dict[CredentialScope, CredentialAuthenticator] = {
            CREDENTIAL_SCOPE: PiOpenRouterAuthenticator(),
        }

    @property
    def command_builder(self) -> PiCommandBuilder:
        return self._command_builder

    @property
    def workspace_adapter(self) -> PiWorkspaceAdapter:
        return self._workspace_adapter

    @property
    def transcript_reader(self) -> PiTranscriptReader:
        return self._transcript_reader

    @property
    def authenticators(self) -> Mapping[CredentialScope, CredentialAuthenticator]:
        return self._authenticators

    def create_runner(self, ctx: HarnessRunContext, **kwargs) -> PiSubprocessRunner:
        return PiSubprocessRunner(
            context=ctx, command_builder=self._command_builder, **kwargs
        )
