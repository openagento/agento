"""Hidden ``token:*`` aliases for the renamed ``credential:*`` commands.

Kept for exactly one release cycle so scripts and runbooks referencing ``token:list``
keep working; removal is tracked in ROADMAP.md. Each alias subclasses the real command,
so behaviour cannot drift — only ``name``/``shortcut``/``hidden`` differ.
"""
from __future__ import annotations

from .credential import (
    CredentialDeregisterCommand,
    CredentialListCommand,
    CredentialMarkErrorCommand,
    CredentialRefreshCommand,
    CredentialRegisterCommand,
    CredentialResetCommand,
    CredentialSetPriorityCommand,
    CredentialUsageCommand,
)

_ALIASES = [
    (CredentialRegisterCommand, "token:register", "to:reg"),
    (CredentialRefreshCommand, "token:refresh", "to:ref"),
    (CredentialListCommand, "token:list", "to:li"),
    (CredentialDeregisterCommand, "token:deregister", "to:de"),
    (CredentialMarkErrorCommand, "token:mark-error", "to:me"),
    (CredentialResetCommand, "token:reset", "to:res"),
    (CredentialSetPriorityCommand, "token:set-priority", "to:sp"),
    (CredentialUsageCommand, "token:usage", "to:us"),
]


def _make_alias(base: type, alias_name: str, alias_shortcut: str) -> type:
    return type(
        f"Legacy{base.__name__}",
        (base,),
        {
            "name": property(lambda self, _n=alias_name: _n),
            "shortcut": property(lambda self, _s=alias_shortcut: _s),
            "hidden": property(lambda self: True),
            "__doc__": f"Deprecated alias for {base().name}.",
        },
    )


LEGACY_TOKEN_COMMANDS = [
    _make_alias(base, name, shortcut) for base, name, shortcut in _ALIASES
]

__all__ = ["LEGACY_TOKEN_COMMANDS"]
