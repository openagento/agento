"""Helpers for interpreting showIn* scope-restriction flags on system.json fields.

Magento-style: fields can declare `showInDefault` / `showInWorkspace` /
`showInAgentView` booleans to restrict which scopes allow editing. Missing
flags default to True (backward compatible — field editable at any scope).
"""
from __future__ import annotations

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
