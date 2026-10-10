"""CLI command registry — modules contribute commands via di.json."""
from __future__ import annotations

import argparse
import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Command(Protocol):
    """Protocol for module-contributed CLI commands (Magento console command pattern)."""

    @property
    def name(self) -> str:
        """Subcommand name (e.g. 'sync', 'publish')."""
        ...

    @property
    def shortcut(self) -> str:
        """Short alias (e.g. 'se:up' for 'setup:upgrade'). Empty string = no shortcut."""
        ...

    @property
    def help(self) -> str:
        """Help text for argparse."""
        ...

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add arguments to the subparser."""
        ...

    def execute(self, args: argparse.Namespace) -> None:
        """Execute the command."""
        ...


# Namespace short-forms allowed as the first shortcut segment instead of the
# default first-2-letters (avoided the old token:list / tool:list collision; keeps the
# multi-word agent_view readable).
NAMESPACE_ALIASES: dict[str, str] = {"tool": "tl", "agent_view": "av"}


def _segment_code(segment: str, *, is_first: bool) -> str:
    """Derive the canonical shortcut code for a single command-name segment.

    First segment may use a registered namespace alias. Hyphenated segments
    use the first letter of each hyphen-part. Everything else is first 2 letters.
    """
    if is_first and segment in NAMESPACE_ALIASES:
        return NAMESPACE_ALIASES[segment]
    if "-" in segment:
        return "".join(part[0] for part in segment.split("-") if part)
    return segment[:2]


def is_valid_shortcut(name: str, shortcut: str) -> bool:
    """True if ``shortcut`` is derivable from ``name`` per the documented rule.

    Rule: split both on ':' into matching segments. The first segment matches a
    namespace alias or the first 2 letters. Hyphenated segments match their
    hyphen-part initials. Any other segment is a prefix of the word (length >= 2)
    — normally exactly 2 letters, extended only to break a collision (e.g.
    'reset' -> 'res'). Empty shortcut = no shortcut (valid).
    """
    if not shortcut:
        return True
    name_parts = name.split(":")
    sc_parts = shortcut.split(":")
    if len(name_parts) != len(sc_parts):
        return False
    for idx, (seg, sc) in enumerate(zip(name_parts, sc_parts, strict=True)):
        is_first = idx == 0
        if is_first and seg in NAMESPACE_ALIASES:
            if sc != NAMESPACE_ALIASES[seg]:
                return False
            continue
        if "-" in seg:
            if sc != _segment_code(seg, is_first=is_first):
                return False
            continue
        if len(sc) < 2 or not seg.startswith(sc):
            return False
    return True


# Registry: name -> Command instance
_COMMANDS: dict[str, Command] = {}

# Shortcut registry: shortcut -> command name
_SHORTCUTS: dict[str, str] = {}


def register_command(command: Command) -> None:
    """Register a command with optional shortcut."""
    _COMMANDS[command.name] = command
    sc = command.shortcut
    if sc:
        if sc in _SHORTCUTS:
            logger.warning(
                "Shortcut %r already registered for %r, ignoring duplicate from %r",
                sc, _SHORTCUTS[sc], command.name,
            )
        else:
            _SHORTCUTS[sc] = command.name


def get_commands() -> dict[str, Command]:
    """Return all registered commands."""
    return dict(_COMMANDS)


def get_shortcuts() -> dict[str, str]:
    """Return all registered shortcuts (shortcut -> command name)."""
    return dict(_SHORTCUTS)


def resolve_shortcut(name: str) -> str:
    """Translate a shortcut to its full command name, or return input unchanged."""
    return _SHORTCUTS.get(name, name)


def clear() -> None:
    """Reset registry (for testing)."""
    _COMMANDS.clear()
    _SHORTCUTS.clear()
