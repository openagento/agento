"""Resolution of declared tool-enablement relationships (``requires``).

A ``module.json`` ``tools[]`` entry may declare ``requires: "<other tool>"``, meaning
it is available only if that tool is enabled too. The toolbox enforces the same
declaration in ``registerTools``' gate; this module is the Python side, shared by the
admin Tools screen and ``tool:list`` so neither can drift from the runtime.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def scan_tool_requires() -> dict[str, str]:
    """Declared ``name -> requires`` for every enabled module's tools.

    Same manifest scan as the admin Tools screen uses for grouping (``scan_modules``
    directly, no ``bootstrap()``), so both see the same relationships the toolbox
    gate enforces.
    """
    from .bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
    from .module_loader import scan_modules
    from .module_status import filter_enabled

    requires: dict[str, str] = {}
    for modules_dir in (CORE_MODULES_DIR, USER_MODULES_DIR):
        if not Path(modules_dir).is_dir():
            continue
        try:
            manifests = filter_enabled(scan_modules(modules_dir))
        except Exception:
            continue
        for manifest in manifests:
            for tool in manifest.tools:
                name, req = tool.get("name"), tool.get("requires")
                if name and isinstance(req, str) and req:
                    requires[name] = req
    return requires


def blocked_by(
    name: str, requires: dict[str, str], resolve: Callable[[str], bool]
) -> str | None:
    """The nearest ancestor of ``name`` in the requires chain that is not enabled.

    ``resolve(tool_name)`` reports a tool's OWN resolved value. ``name``'s own value is
    deliberately not considered — that is plain "disabled", not "blocked".

    Fails closed on a cycle by naming the node that closes it: the toolbox gate returns
    false for a cycle, so this must never answer "unblocked". ``module:validate`` rejects
    cycles, making this the defensive path for a manifest that bypassed validation.
    """
    seen = {name}
    current = requires.get(name)
    while current is not None:
        if current in seen:
            return current
        if not resolve(current):
            return current
        seen.add(current)
        current = requires.get(current)
    return None


def is_effective(
    name: str, requires: dict[str, str], resolve: Callable[[str], bool]
) -> bool:
    """Whether ``name`` is actually available: its own value AND its whole chain."""
    return resolve(name) and blocked_by(name, requires, resolve) is None
