"""CliInvoker protocol and registry — agent-agnostic CLI command construction.

Each agent module (claude, codex, etc.) implements CliInvoker and registers it
via ``di.json`` under ``cli_invokers``. Framework lookups go through this
registry so ``agento run`` never branches on a provider literal.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from .agent_manager.models import AgentProvider

logger = logging.getLogger(__name__)


@runtime_checkable
class CliInvoker(Protocol):
    """How to invoke an agent's CLI binary in interactive or headless mode."""

    def interactive_command(self, *, yolo: bool = False) -> list[str]:
        """Command to spawn the CLI in interactive TTY mode (no prompt).

        When ``yolo`` is True, include the provider's skip-approvals/bypass flag
        so the interactive session runs without per-action approval prompts —
        the same bypass the headless/job path always uses.
        """
        ...

    def headless_command(
        self, prompt: str, *, model: str | None = None,
    ) -> list[str]:
        """Command to run the CLI in one-shot headless mode with the given prompt."""
        ...


_CLI_INVOKERS: dict[AgentProvider, CliInvoker] = {}


def register_cli_invoker(provider: AgentProvider, invoker: CliInvoker) -> None:
    """Register a CliInvoker for an agent provider."""
    _CLI_INVOKERS[provider] = invoker
    logger.debug("Registered CLI invoker for provider %s", provider.value)


def get_cli_invoker(provider: AgentProvider | str) -> CliInvoker:
    """Look up the CliInvoker for a provider.

    Accepts AgentProvider or a provider string. Raises ValueError if the string
    isn't a known provider, KeyError if no invoker is registered.
    """
    if isinstance(provider, str):
        provider = AgentProvider(provider)
    invoker = _CLI_INVOKERS.get(provider)
    if invoker is None:
        raise KeyError(
            f"No CliInvoker registered for provider {provider!r}. "
            f"Registered: {list(_CLI_INVOKERS.keys())}. Has bootstrap() been called?"
        )
    return invoker


def clear() -> None:
    """Reset registry (for testing)."""
    _CLI_INVOKERS.clear()
