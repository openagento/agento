"""Helpers for interpreting showIn* scope-restriction flags on system.json fields.

Magento-style: fields can declare `showInDefault` / `showInWorkspace` /
`showInAgentView` booleans to restrict which scopes allow editing. Missing
flags default to True (backward compatible — field editable at any scope).
"""
from __future__ import annotations

import math

from .scoped_config import Scope

_SCOPE_TO_FLAG: dict[str, str] = {
    Scope.DEFAULT: "showInDefault",
    Scope.WORKSPACE: "showInWorkspace",
    Scope.AGENT_VIEW: "showInAgentView",
}


def is_scope_allowed(field_schema: dict, scope: str) -> bool:
    """Return True if the field may be edited at the given scope."""
    flag = _SCOPE_TO_FLAG.get(scope)
    if flag is None:
        return True
    return bool(field_schema.get(flag, True))


def allowed_scopes(field_schema: dict) -> list[str]:
    """Return scopes where the field is editable, in default→workspace→agent_view order."""
    return [
        scope
        for scope, flag in _SCOPE_TO_FLAG.items()
        if bool(field_schema.get(flag, True))
    ]


class ToolboxOnlyConfigError(RuntimeError):
    """Raised when Python is asked for a value only the toolbox may hold.

    The toolbox is the single container with secrets. A field marked
    ``access: "toolbox_only"`` must never be resolved — let alone decrypted —
    on the Python side, so asking for one is a programming error, not a
    missing value. Returning ``None`` instead would let a caller silently
    treat "you may not have this" as "it is not configured".
    """


def is_toolbox_only(field_schema: dict) -> bool:
    """True when only the toolbox may resolve this field."""
    return field_schema.get("access") == "toolbox_only"


def env_allowed(field_schema: dict) -> bool:
    """True when a ``CONFIG__*`` environment variable may supply this field.

    A secret in the process environment is readable by every child process and
    lands in crash dumps and `ps` output, so a field may opt out with
    ``allowEnv: false`` and be settable through the DB only.
    """
    return field_schema.get("allowEnv", True) is not False


# Every restricted field ever SCANNED in this process, path -> its schema. Populated by
# bootstrap from all scanned modules (not only the enabled ones) and never shrunk.
#
# The security metadata is the only thing standing between Python and a decrypted secret,
# so it must not depend on the live manifest registry being populated and correct at the
# moment of the read: a re-bootstrap that fails part-way, a module that was disabled, or a
# path whose module is not in the registry would otherwise make the field look unrestricted
# and fall open. A sticky registry answers "this path is restricted" even then.
_RESTRICTED_FIELDS: dict[str, dict] = {}


def _is_restricted(field_schema: dict) -> bool:
    return is_toolbox_only(field_schema) or not env_allowed(field_schema)


def remember_restricted_fields(module_name: str, module_config: dict | None) -> None:
    """Record the restricted fields a scanned module declares. Never forgets one."""
    for field_name, field_schema in (module_config or {}).items():
        if isinstance(field_schema, dict) and _is_restricted(field_schema):
            _RESTRICTED_FIELDS[f"{module_name}/{field_name}"] = field_schema


def restricted_schema(path: str) -> dict | None:
    """The remembered schema for ``path``, or None if it was never a restricted field."""
    return _RESTRICTED_FIELDS.get(path)


def clear_restricted_fields() -> None:
    """Test-only: drop the remembered registry."""
    _RESTRICTED_FIELDS.clear()


def numeric_bound_error(field_name: str, field_def: dict, value) -> str | None:
    """The bound violation in ``value`` for this field, or None (PRD E3-E5 §3.3).

    One rule, three callers: ``config:set``, ``module:validate`` (the
    ``config.json`` default) and the resolver's ENV/DB substitution. A second copy of
    the comparison is a second place for the two to disagree.
    """
    minimum = field_def.get("min")
    maximum = field_def.get("max")
    try:
        number = int(str(value).strip()) if field_def.get("type") == "integer" else float(str(value).strip())
    except (TypeError, ValueError):
        # Only a field that declares a bound is refused for being unparseable: an
        # unbounded numeric field has never been checked and is not this change's to break.
        if minimum is None and maximum is None:
            return None
        return f"'{value}' is not a number for field '{field_name}'"
    if (minimum is not None or maximum is not None) and not math.isfinite(number):
        # NaN compares False against BOTH bounds, so a range check alone lets it through
        # and the field ends up holding a value no comparison will ever refuse again;
        # an infinity passes a one-sided bound the same way. A bound that cannot refuse
        # is not a bound (SEC-9), so this is decided before the comparison, not by it.
        # Gated on a declared bound for the same reason as the unparseable case above:
        # an unbounded numeric field has never been checked here.
        return f"'{value}' is not a finite number for field '{field_name}'"
    if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        # Name the whole allowed range, not only the bound that was crossed: an operator
        # correcting the value needs to know where the other end is.
        low = "-inf" if minimum is None else minimum
        high = "inf" if maximum is None else maximum
        return f"{number} is outside the allowed range [{low}, {high}] for field '{field_name}'"
    return None
