"""One config write path for ``config:set``, the admin TUI and the web admin API.

Validation raises ``ConfigWriteError`` with the text ``config:set`` prints. ``save_config``
validates, writes with dependent repair, commits and dispatches ``config_save_after``.

``allow_secret=False`` is the web form: ``web`` holds no encryption key and must never
write a secret, so it accepts a path only when it can PROVE the field is not one.
"""
from __future__ import annotations

import json
import re

from .config_schema import numeric_bound_error
from .config_schema_options import field_options
from .scoped_config import Scope

# The runtime gate keys (no module prefix, no schema): `tool:enable` and `skill:enable` are their CLI.
# A skill name may carry a dash; it must match a skill_registry row exactly.
_GATE_KEY = re.compile(r"(tools)/([a-z][a-z0-9_]*)/is_enabled|(skill)/([^/]+)/is_enabled")
_USE_CLI = "set this field with bin/agento config:set"


class ConfigWriteError(ValueError):
    """A refused config write; the message is safe to show an admin."""


def _read_json(path):
    return json.loads(path.read_text())


def validate_config_path(path: str, scope: str = Scope.DEFAULT) -> None:
    """Field existence plus the ``showIn*`` scope flags of ``system.json`` / tool fields."""
    from .config_schema import allowed_scopes, is_scope_allowed
    from .core_config import _find_module_dir, _parse_config_path

    parsed = _parse_config_path(path)
    if parsed is None:
        raise ConfigWriteError(f"Error: Invalid config path '{path}'.")
    module_name, tool_name, field_name = parsed

    module_dir = _find_module_dir(module_name)
    if module_dir is None:
        raise ConfigWriteError(f"Error: Module '{module_name}' not found.")

    if tool_name is not None:
        field_def = load_tool_field_schema(module_dir, tool_name, field_name)
        if field_def is not None and not is_scope_allowed(field_def, scope):
            scopes = ", ".join(allowed_scopes(field_def)) or "none"
            raise ConfigWriteError(
                f"Error: Field '{field_name}' cannot be set at scope '{scope}' (allowed: {scopes})"
            )
        return

    # field_name may contain '/' (slash-keyed schema fields).
    system_path = module_dir / "system.json"
    if not system_path.exists():
        return
    try:
        system = _read_json(system_path)
    except (ValueError, OSError):
        return
    if field_name not in system:
        known = ", ".join(sorted(system.keys()))
        raise ConfigWriteError(
            f"Error: Field '{field_name}' not found in {module_name}/system.json\n"
            f"  Available fields: {known}"
        )
    field_def = system[field_name]
    if isinstance(field_def, dict) and not is_scope_allowed(field_def, scope):
        scopes = ", ".join(allowed_scopes(field_def)) or "none"
        raise ConfigWriteError(
            f"Error: Field '{field_name}' cannot be set at scope '{scope}' (allowed: {scopes})"
        )


def load_tool_field_schema(module_dir, tool_name: str, field_name: str) -> dict | None:
    """Return schema dict for a tool field, or None if not discoverable."""
    manifest_path = module_dir / "module.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = _read_json(manifest_path)
    except (ValueError, OSError):
        return None
    for tool in manifest.get("tools", []):
        if tool.get("name") == tool_name:
            field = tool.get("fields", {}).get(field_name)
            return field if isinstance(field, dict) else None
    return None


def _effective_depends_on_value(conn, depends_on: str, *, scope: str, scope_id: int) -> str | None:
    """The value a dependent select is narrowed by, resolved AT THE TARGET SCOPE.

    Must honour the full precedence chain (ENV > agent_view > workspace > default >
    config.json), not just a raw DB row at this scope: a view inheriting its harness from
    the default scope, or one set only via ``CONFIG__AGENT_VIEW__HARNESS``, still has an
    effective harness that must narrow the provider list.
    """
    from .config_resolver import read_config_defaults
    from .core_config import _find_module_dir
    from .scoped_config import ORIGIN_ABSENT, resolve_with_origin

    module, _, field = depends_on.partition("/")
    module_dir = _find_module_dir(module)
    defaults = read_config_defaults(module_dir) if module_dir is not None else {}
    json_value = (defaults or {}).get(field) if field else None

    value, origin = resolve_with_origin(
        conn, depends_on,
        agent_view_id=scope_id if scope == Scope.AGENT_VIEW else None,
        workspace_id=scope_id if scope == Scope.WORKSPACE else None,
        config_json_value=str(json_value) if json_value is not None else None,
    )
    return None if origin == ORIGIN_ABSENT else value


def validate_config_value(path: str, value: str, *, conn=None, scope: str | None = None, scope_id: int = 0) -> None:
    """Select options and private-key parsing, against ``system.json``."""
    from .core_config import _find_module_dir, _parse_config_path

    parsed = _parse_config_path(path)
    if parsed is None:
        return
    module_name, tool_name, field_name = parsed
    if tool_name is not None:
        # Tool-field option validation is not handled here today; keep behaviour.
        return
    module_dir = _find_module_dir(module_name)
    if module_dir is None or not (module_dir / "system.json").exists():
        return
    try:
        system = _read_json(module_dir / "system.json")
    except (ValueError, OSError):
        return
    field_def = system.get(field_name)
    if not isinstance(field_def, dict):
        return

    # Declarative rules that apply to any raw value (e.g. maxLength) live in ONE shared
    # helper, which the admin editor calls too — a rule implemented in only one entry
    # point is not a rule. They run BEFORE the type-specific checks so an oversized value
    # is reported as oversized, not as whatever the parse made of its first 16 KB.
    from .config_validation import validate_field_value

    generic_error = validate_field_value(field_def, value)
    if generic_error is not None:
        raise ConfigWriteError(f"Error: {generic_error} ('{field_name}')")

    if is_private_key_field(field_name, field_def):
        _validate_private_key(field_name, value)
        return

    field_type = field_def.get("type")

    if field_type in ("integer", "number"):
        _validate_numeric_bounds(field_name, field_def, value)
        return

    if field_type not in ("select", "multiselect"):
        return

    # For a dynamic select (harness / provider) the allowed values come from the
    # agent_harnesses declarations on disk — config:set has no bootstrap() available.
    # A DEPENDENT select (provider depends on harness) must be narrowed to the harness
    # actually in effect at this scope; validating against the union of every harness's
    # providers would accept `(claude, openai)` and only fail at runtime.
    depends_on = field_def.get("depends_on")
    depends_value = None
    if depends_on and conn is not None and scope is not None:
        depends_value = _effective_depends_on_value(conn, depends_on, scope=scope, scope_id=scope_id)
    options = field_options(field_def, depends_on_value=depends_value)
    allowed = [opt["value"] for opt in options if isinstance(opt, dict) and "value" in opt]
    # A multiselect is stored comma-separated; "" selects none.
    members = (value.split(",") if value else []) if field_type == "multiselect" else [value]
    if len(set(members)) != len(members) or any(m not in allowed for m in members):
        raise ConfigWriteError(
            f"Error: Invalid value '{value}' for {field_type} field '{field_name}'\n"
            f"  Allowed values: {', '.join(allowed)}"
        )


def _validate_numeric_bounds(field_name: str, field_def: dict, value: str) -> None:
    error = numeric_bound_error(field_name, field_def, value)
    if error is not None:
        raise ConfigWriteError(f"Error: {error}")


def is_private_key_field(field_name: str, field_def: dict) -> bool:
    """An ``obscure`` field whose name ends in ``ssh_private_key``.

    Name-suffix matching, not a hardcoded ``agent_view/…`` path, so the
    framework stays module-agnostic and a third-party module that follows the
    same naming gets the same protection.
    """
    return field_def.get("type") == "obscure" and field_name.rsplit("/", 1)[-1] == "ssh_private_key"


def _validate_private_key(field_name: str, value: str) -> None:
    """Reject a private-key value that cannot parse.

    A corrupted interactive paste once stored a 36-byte value (the BEGIN header
    alone), which `identity:show` happily fingerprinted and `workspace_build`
    materialized — four silent builds of `Permission denied (publickey)`.
    Nothing legitimate fails this check: `config:set … < id_rsa` always yields a
    parsable key. Never echoes the value.
    """
    from .ssh_keys import EncryptedKeyError, derive_public_key

    if not value.strip():
        return  # clearing the field stays possible
    try:
        derive_public_key(value)
    except EncryptedKeyError:
        raise ConfigWriteError(
            f"Error: value for '{field_name}' is a passphrase-protected private "
            f"key. The agent runs unattended and cannot unlock it — store an "
            f"unencrypted key."
        ) from None
    except ValueError as e:
        raise ConfigWriteError(
            f"Error: value for '{field_name}' does not parse as an SSH private key: {e}\n"
            "  Set it from a file rather than an interactive paste:\n"
            "    agento config:set <path> --agent-view <code> < id_rsa"
        ) from None


def _gate_key_names() -> set[str]:
    """Every tool name a manifest declares, plus every key a declared tool ``requires``."""
    from .bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
    from .module_discovery import scan_all_modules

    names = set()
    for m in scan_all_modules(CORE_MODULES_DIR, USER_MODULES_DIR):
        for tool in m.tools:
            names.update(n for n in (tool.get("name"), tool.get("requires")) if isinstance(n, str))
    return names


def _skill_registered(conn, name: str) -> bool:
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM skill_registry WHERE name = %s", (name,))
            row = cur.fetchone()
    except Exception:
        return False  # no skill table (module disabled): nothing is proven registered
    # Compared here, not by the table's case-insensitive collation: the gate key is read verbatim.
    return row is not None and row["name"] == name


def _is_gate_key(conn, path: str) -> bool:
    """True for a gate key naming a declared tool or a registered skill; raises for one naming nothing."""
    gate = _GATE_KEY.fullmatch(path)
    if gate is None:
        return False
    tool, skill = gate.group(2), gate.group(4)
    if tool is not None and tool not in _gate_key_names():
        raise ConfigWriteError(f"no module declares tool {tool!r}")
    if skill is not None and not _skill_registered(conn, skill):
        raise ConfigWriteError(f"no skill {skill!r} is registered")
    return True


def validate_web_path(conn, path: str) -> bool:
    """The web form's path check, for a write and a delete alike: a gate key, or a field provably
    not a secret. Returns True for a gate key, whose value is then ``0`` or ``1`` only."""
    if not isinstance(path, str):
        raise ConfigWriteError("path must be a string")
    if _is_gate_key(conn, path):
        return True
    _prove_not_secret(path)
    return False


def _prove_not_secret(path: str) -> None:
    """Refuse unless the field's schema is an object that is provably not a secret."""
    from .config_schema import is_secret_field
    from .core_config import _find_module_dir, _parse_config_path

    parsed = _parse_config_path(path)
    module_dir = _find_module_dir(parsed[0]) if parsed else None
    if parsed is None or module_dir is None:
        raise ConfigWriteError(_USE_CLI)
    _module, tool_name, field_name = parsed
    try:
        manifest = _read_json(module_dir / "module.json")
        system = _read_json(module_dir / "system.json") if (module_dir / "system.json").exists() else {}
    except (ValueError, OSError):
        raise ConfigWriteError(_USE_CLI) from None
    if tool_name is not None:
        tool = next((t for t in manifest.get("tools", []) if t.get("name") == tool_name), None)
        schema = ((tool or {}).get("fields") or {}).get(field_name)
    else:
        schema = system.get(field_name) if isinstance(system, dict) else None
    if is_secret_field(schema):
        raise ConfigWriteError(_USE_CLI)


def validate_config_write(conn, path: str, value: str, scope: str, scope_id: int, *, allow_secret: bool) -> None:
    if not isinstance(path, str) or not isinstance(value, str):
        raise ConfigWriteError("path and value must be strings")
    if not allow_secret and validate_web_path(conn, path):
        if value not in ("0", "1"):
            raise ConfigWriteError("is_enabled must be 0 or 1")
        return
    validate_config_path(path, scope)
    validate_config_value(path, value, conn=conn, scope=scope, scope_id=scope_id)


def write_config(conn, path: str, value: str, *, scope: str, scope_id: int,
                 actor_id: int | None = None) -> tuple[bool, list[tuple[str, str]]]:
    """Write with dependent repair in one transaction, commit, dispatch ``config_save_after``.

    No validation: the admin TUI calls this directly because it also writes the schema-less
    gate keys (``tools/<name>/is_enabled``, ``skill/<name>/is_enabled``). Everyone else
    calls ``save_config``.
    """
    from .config_dependents import set_config_with_dependents
    from .event_manager import get_event_manager
    from .events import ConfigSavedEvent

    conn.begin()
    try:
        if actor_id is not None:
            from .access.accounts import AccessError, _lock_actor_and

            try:
                with conn.cursor() as cur:
                    _lock_actor_and(cur, actor_id)
            except AccessError as exc:
                raise ConfigWriteError(str(exc)) from None
        encrypted, reset = set_config_with_dependents(conn, path, value, scope=scope, scope_id=scope_id)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    get_event_manager().dispatch("config_save_after", ConfigSavedEvent(path=path, encrypted=encrypted))
    return encrypted, reset


def save_config(conn, path: str, value: str, *, scope: str, scope_id: int, allow_secret: bool,
                actor_id: int | None = None) -> tuple[bool, list[tuple[str, str]]]:
    """Validate, then ``write_config``. Returns ``(encrypted, reset)``."""
    validate_config_write(conn, path, value, scope, scope_id, allow_secret=allow_secret)
    return write_config(conn, path, value, scope=scope, scope_id=scope_id, actor_id=actor_id)
