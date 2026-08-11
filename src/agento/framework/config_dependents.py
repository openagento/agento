"""Saving a config value that other fields depend on, as ONE operation.

``agent_view/provider`` declares ``depends_on: agent_view/harness``, so writing the
harness can invalidate the provider. Both writers — ``config:set`` and the admin TUI —
must apply the same repair, or the TUI leaves exactly the broken pair the CLI prevents
(``harness=codex`` with ``provider=anthropic``, which fails at job-resolution time).
Hence a single entry point instead of the rule living in each caller.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def set_config_with_dependents(
    conn,
    path: str,
    value: str,
    *,
    scope: str,
    scope_id: int = 0,
) -> tuple[bool, list[tuple[str, str]]]:
    """Write ``path`` and repair any dependent field the new value invalidates.

    Returns ``(encrypted, [(dependent_path, new_value)])``. Does NOT commit — the caller
    owns the transaction, so the parent write and the repair land together and a broken
    pair is never observable.
    """
    from .core_config import config_set_auto_encrypt

    encrypted = config_set_auto_encrypt(
        conn, path, value, scope=scope, scope_id=scope_id
    )
    changed = reset_dependents(conn, path, value, scope=scope, scope_id=scope_id)
    return encrypted, changed


def reset_dependents(
    conn, parent_path: str, parent_value: str, *, scope: str, scope_id: int = 0,
) -> list[tuple[str, str]]:
    """Re-point any dependent select whose current value the new parent invalidates.

    Returns ``[(path, new_value)]`` for whatever was rewritten. Only fields declaring
    ``depends_on == parent_path`` are considered, and only when their effective value is
    no longer among the options the new parent offers.
    """
    from .config_schema_options import field_options
    from .core_config import _find_module_dir, _parse_config_path, config_set_auto_encrypt
    from .scoped_config import ORIGIN_ABSENT, Scope, resolve_with_origin

    parsed = _parse_config_path(parent_path)
    if parsed is None:
        return []
    module_name, tool_name, _field = parsed
    if tool_name is not None:
        return []
    module_dir = _find_module_dir(module_name)
    if module_dir is None or not (module_dir / "system.json").exists():
        return []
    try:
        system = json.loads((module_dir / "system.json").read_text())
    except (ValueError, OSError):
        return []

    changed: list[tuple[str, str]] = []
    for dep_field, dep_def in system.items():
        if not isinstance(dep_def, dict) or dep_def.get("depends_on") != parent_path:
            continue
        options = field_options(dep_def, depends_on_value=parent_value)
        allowed = [o["value"] for o in options if isinstance(o, dict) and "value" in o]
        if not allowed:
            continue
        dep_path = f"{module_name}/{dep_field}"
        current, origin = resolve_with_origin(
            conn, dep_path,
            agent_view_id=scope_id if scope == Scope.AGENT_VIEW else None,
            workspace_id=scope_id if scope == Scope.WORKSPACE else None,
        )
        if origin != ORIGIN_ABSENT and current in allowed:
            continue
        replacement = _preferred_value(parent_path, parent_value, allowed)
        config_set_auto_encrypt(
            conn, dep_path, replacement, scope=scope, scope_id=scope_id
        )
        changed.append((dep_path, replacement))
    return changed


def _preferred_value(parent_path: str, parent_value: str, allowed: list[str]) -> str:
    """The value to repair a dependent with.

    For the harness→provider pair that is the harness's DECLARED ``default_provider``, not
    merely the first option: declaration order is incidental, and picking arbitrarily
    could silently move a view onto a different vendor than the harness intends.
    """
    if parent_path == "agent_view/harness":
        default = _declared_default_provider(parent_value)
        if default in allowed:
            return default
    return allowed[0]


def _declared_default_provider(harness_id: str) -> str | None:
    """``default_provider`` from the harness declaration, read off disk (no bootstrap)."""
    from .harness import enumerate_harness_declarations
    from .module_discovery import resolve_module_root

    try:
        for decl in enumerate_harness_declarations(resolve_module_root()):
            if str(decl.descriptor.id) == harness_id:
                return str(decl.descriptor.default_provider)
    except Exception:
        logger.debug("Could not read default_provider for %r", harness_id, exc_info=True)
    return None


__all__ = ["reset_dependents", "set_config_with_dependents"]
