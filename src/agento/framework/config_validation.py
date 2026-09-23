"""Declarative ``system.json`` field-value validation, shared by every entry point.

``config:set`` (``cli/config.py``) and the admin editor (``admin/screens/config.py``)
both accept a raw value for a ``system.json`` field, and each used to validate it its
own way — the CLI only ``select``/``multiselect``, the editor only ``integer``/``json``.
A constraint implemented in one and not the other is not a constraint, so a rule that
applies to a raw value lives HERE and both call it.

``maxLength`` is measured in UTF-8 **bytes**, not characters: the limits it guards are
byte limits (a secret travelling through the process environment), so a multibyte value
must not slip past a character count.
"""
from __future__ import annotations

# Free-form text types that can carry a length bound. A `select` value is bounded by
# its options, and `integer`/`json` carry their own type check.
MAX_LENGTH_TYPES = frozenset({"string", "obscure", "textarea"})


def max_length_of(field_def: dict) -> int | None:
    """The field's usable ``maxLength``, or ``None`` when it declares none.

    A malformed declaration is ignored here and reported by ``module:validate``
    instead — the shared validator must never raise on a manifest typo.
    """
    if not isinstance(field_def, dict):
        return None
    if field_def.get("type", "string") not in MAX_LENGTH_TYPES:
        return None
    raw = field_def.get("maxLength")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return None
    return raw


def validate_field_value(field_def: dict, value: str) -> str | None:
    """Return an error message, or ``None`` when ``value`` is acceptable.

    ``field_def`` is the raw ``system.json`` field dict, or a normalized subset of it
    carrying at least ``type`` plus the constraint keys (what the admin layer passes).
    """
    limit = max_length_of(field_def)
    if limit is None:
        return None
    size = len((value or "").encode("utf-8"))
    if size > limit:
        return f"Value is {size} bytes, over the {limit}-byte limit for this field"
    return None
