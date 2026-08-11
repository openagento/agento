"""Resolving the option list for a ``system.json`` select field.

Framework-level because both the CLI and the admin TUI consume it, and
``config_dependents`` (a framework module) needs it to decide whether a dependent value is
still valid — a framework module importing ``framework/cli/`` would invert the layering.
"""
from __future__ import annotations


def field_options(
    field_def: dict,
    *,
    depends_on_value: str | None = None,
) -> list[dict[str, str]]:
    """Literal ``options``, or the resolved ``options_source`` for a dynamic select.

    Used wherever select options are consumed (``config:set`` validation, ``config:schema``,
    the admin TUI, dependent-value repair) so a harness list never has to be duplicated into
    ``system.json``.
    """
    literal = field_def.get("options")
    if literal:
        return [o for o in literal if isinstance(o, dict) and "value" in o]
    source = field_def.get("options_source")
    if not source:
        return []
    from .harness import resolve_options

    try:
        return resolve_options(source, depends_on_value=depends_on_value)
    except Exception:  # a missing/broken manifest must not break `config:get`
        return []


__all__ = ["field_options"]
