"""Which harness-owned ``system.json`` fields the agent_view screen shows.

A harness's native-config passthrough (``claude/settings``, ``codex/config``, …) is set
per agent_view, so an operator expects it beside the harness selector — not on the module
node of a harness this view may not even use. The field says so itself::

    "settings": {"type": "textarea", "harness_option": true}

and admin surfaces it under ``agent_view``, hiding it unless the effective
``agent_view/harness`` is one the declaring module declares. Same shape as
``provider_option``, one axis up: the condition lives with the HARNESS module, so no
framework file names a harness.

Declarations are read OFF DISK (``enumerate_harness_declarations``), never from the
runtime registry, so this works in admin without a ``bootstrap()``.
"""
from __future__ import annotations

from pathlib import Path

HARNESS_OPTION_KEY = "harness_option"


def modules_declaring(harness: str | None, *, project_root: Path | None = None) -> frozenset[str]:
    """The modules whose ``di.json`` declares ``harness``.

    Empty when the id is unknown or unset — callers must read that as "no information",
    never as "hide everything" (see :func:`is_harness_option_hidden`).
    """
    if not harness:
        return frozenset()

    from ..module_discovery import resolve_module_root
    from .manifest import enumerate_harness_declarations

    if project_root is None:
        project_root = resolve_module_root()
    return frozenset(
        declaration.module
        for declaration in enumerate_harness_declarations(project_root)
        if str(declaration.descriptor.id) == harness
    )


def is_harness_option_hidden(
    field_schema: dict,
    *,
    module: str,
    harness: str | None,
    project_root: Path | None = None,
) -> bool:
    """True when this field belongs to a harness the view does not use.

    Hides only on POSITIVE knowledge, like :func:`is_provider_option_hidden`: an unset or
    unresolvable harness leaves every passthrough visible, because hiding a field the
    operator still needs to set is the worse failure.
    """
    if not field_schema.get(HARNESS_OPTION_KEY):
        return False
    declared = modules_declaring(harness, project_root=project_root)
    if not declared:
        return False
    return module not in declared
