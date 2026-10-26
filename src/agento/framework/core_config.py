"""CRUD operations for core_config_data table (Magento-like config storage)."""
from __future__ import annotations

import json
from pathlib import Path

from .encryptor import get_encryptor
from .scoped_config import Scope


def config_set(conn, path: str, value: str, *, encrypted: bool = False) -> None:
    """Set a config value (INSERT or UPDATE)."""
    stored_value = get_encryptor().encrypt(value) if encrypted else value
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO core_config_data (scope, scope_id, path, value, encrypted)
               VALUES ('default', 0, %s, %s, %s)
               ON DUPLICATE KEY UPDATE value = VALUES(value), encrypted = VALUES(encrypted)""",
            (path, stored_value, int(encrypted)),
        )


def config_get(conn, path: str) -> list[dict]:
    """Get config values for a path across all scopes.

    Returns list of {scope, scope_id, value, encrypted} dicts, ordered by specificity.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scope, scope_id, value, encrypted FROM core_config_data WHERE path = %s ORDER BY scope, scope_id",
            (path,),
        )
        rows = cur.fetchall()
    results = []
    for row in rows:
        if isinstance(row, dict):
            scope, sid, value, enc = row["scope"], row["scope_id"], row["value"], row["encrypted"]
        else:
            scope, sid, value, enc = row
        results.append({
            "scope": scope, "scope_id": sid, "value": value,
            "encrypted": bool(enc), "obscure": bool(enc) or is_path_obscure(path),
        })
    return results


def config_get_tree(conn, prefix: str) -> list[dict]:
    """Get all config rows matching prefix across all scopes, with scope labels.

    Returns [{scope, scope_id, scope_label, path, value, encrypted}].
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scope, scope_id, path, value, encrypted FROM core_config_data "
            "WHERE path LIKE %s ORDER BY path, scope, scope_id",
            (prefix.replace("-", "_") + "%",),
        )
        rows = cur.fetchall()

    # Collect scope_ids that need label resolution
    ws_ids = set()
    av_ids = set()
    for row in rows:
        r = row if isinstance(row, dict) else dict(zip(
            ("scope", "scope_id", "path", "value", "encrypted"), row, strict=False
        ))
        if r["scope"] == Scope.WORKSPACE:
            ws_ids.add(r["scope_id"])
        elif r["scope"] == Scope.AGENT_VIEW:
            av_ids.add(r["scope_id"])

    # Resolve labels
    labels = {}
    if ws_ids:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, code, label FROM workspace WHERE id IN ({','.join('%s' for _ in ws_ids)})",
                tuple(ws_ids),
            )
            for r in cur.fetchall():
                labels[(Scope.WORKSPACE, r["id"])] = r["code"]
    if av_ids:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, code, label FROM agent_view WHERE id IN ({','.join('%s' for _ in av_ids)})",
                tuple(av_ids),
            )
            for r in cur.fetchall():
                labels[(Scope.AGENT_VIEW, r["id"])] = r["code"]

    results = []
    for row in rows:
        if isinstance(row, dict):
            scope, sid, path, value, enc = (
                row["scope"], row["scope_id"], row["path"], row["value"], row["encrypted"],
            )
        else:
            scope, sid, path, value, enc = row
        display = "****" if (enc or is_path_obscure(path)) else value
        scope_label = labels.get((scope, sid), "")
        results.append({
            "scope": scope, "scope_id": sid, "scope_label": scope_label,
            "path": path, "value": display, "encrypted": bool(enc),
        })
    return results


def config_delete(
    conn, path: str, *, scope: str = Scope.DEFAULT, scope_id: int = 0
) -> bool:
    """Delete a single config override. Returns True if row existed."""
    if not path or "/" not in path:
        raise ValueError(f"Invalid config path: {path!r} (expected module/field format)")
    if scope not in (Scope.DEFAULT, Scope.WORKSPACE, Scope.AGENT_VIEW):
        raise ValueError(f"Invalid scope: {scope!r}")
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM core_config_data WHERE scope = %s AND scope_id = %s AND path = %s",
            (scope, scope_id, path),
        )
        return cur.rowcount > 0


def config_list(conn, prefix: str = "") -> list[dict]:
    """List all config entries across all scopes, optionally filtered by path prefix."""
    with conn.cursor() as cur:
        if prefix:
            cur.execute(
                "SELECT scope, scope_id, path, value, encrypted FROM core_config_data "
                "WHERE path LIKE %s ORDER BY path, scope, scope_id",
                (prefix.replace("-", "_") + "%",),
            )
        else:
            cur.execute(
                "SELECT scope, scope_id, path, value, encrypted FROM core_config_data "
                "ORDER BY path, scope, scope_id"
            )
        rows = cur.fetchall()

    result = []
    for row in rows:
        if isinstance(row, dict):
            scope, sid, path, value, enc = (
                row["scope"], row["scope_id"], row["path"], row["value"], row["encrypted"]
            )
        else:
            scope, sid, path, value, enc = row
        obscure = bool(enc) or is_path_obscure(path)
        display = "****" if obscure else value
        result.append({
            "scope": scope, "scope_id": sid, "path": path,
            "value": display, "encrypted": bool(enc), "obscure": obscure,
        })
    return result


def _find_module_dir(module_name: str) -> Path | None:
    """Find a module directory across core and user module paths."""
    from .bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
    from .module_discovery import module_dirs_by_name

    # The ONE selection (see module_discovery): a hand-written root list read the CORE copy
    # of a name that app/code overrides, so config resolved against the shadowed schema.
    dirs = dict(module_dirs_by_name(CORE_MODULES_DIR, USER_MODULES_DIR))
    for name_variant in (module_name.replace("_", "-"), module_name):
        if name_variant in dirs:
            return dirs[name_variant]
    return None


def _parse_config_path(path: str) -> tuple[str, str | None, str] | None:
    """Parse a config path into (module, tool_or_None, field).

    Shapes:
      "module/tools/{tool}/{field}"       -> (module, tool, field)   (tool form)
      "module/{field-with-or-without-/}"  -> (module, None, field)   (module form)

    Returns None for malformed paths (no '/', trailing '/', empty module/field,
    or `tools/` prefix that is not exactly 4-part).

    Note: the schema permits ``/`` inside a field name (e.g. agent_view's
    ``identity/ssh_private_key``), so the module-form field may itself contain
    slashes — it is matched verbatim against ``system.json`` keys.
    """
    if "/" not in path:
        return None
    module, _, rest = path.partition("/")
    if not module or not rest or rest.endswith("/"):
        return None
    head, sep, tail = rest.partition("/")
    if head == "tools":
        if not sep or not tail or "/" not in tail:
            return None
        tool, _, field = tail.partition("/")
        if not tool or not field or "/" in field:
            return None
        return module, tool, field
    return module, None, rest


def _is_obscure_field(module_name: str, tool_name: str, field_name: str) -> bool:
    """Check if a field is marked as obscure in module.json."""
    module_dir = _find_module_dir(module_name)
    if module_dir is None:
        return False

    manifest_path = module_dir / "module.json"
    if not manifest_path.exists():
        return False

    try:
        manifest = json.loads(manifest_path.read_text())
        for tool in manifest.get("tools", []):
            if tool["name"] == tool_name:
                field = tool.get("fields", {}).get(field_name, {})
                return field.get("type") == "obscure"
    except (json.JSONDecodeError, KeyError):
        pass
    return False


def _is_obscure_module_config(module_name: str, field_name: str) -> bool:
    """Check if a module-level config field is marked as obscure in system.json."""
    module_dir = _find_module_dir(module_name)
    if module_dir is None:
        return False

    # Prefer system.json, fall back to module.json["config"] for backward compat
    system_path = module_dir / "system.json"
    try:
        if system_path.exists():
            system = json.loads(system_path.read_text())
            field = system.get(field_name, {})
        else:
            manifest = json.loads((module_dir / "module.json").read_text())
            field = manifest.get("config", {}).get(field_name, {})
        return field.get("type") == "obscure"
    except (json.JSONDecodeError, KeyError, OSError):
        pass
    return False


def is_path_obscure(path: str) -> bool:
    """Check if a config path refers to an obscure field based on schema."""
    parsed = _parse_config_path(path)
    if parsed is None:
        return False
    module_name, tool_name, field_name = parsed
    if tool_name is not None:
        return _is_obscure_field(module_name, tool_name, field_name)
    return _is_obscure_module_config(module_name, field_name)


def config_set_auto_encrypt(
    conn, path: str, value: str, *, scope: str = Scope.DEFAULT, scope_id: int = 0
) -> bool:
    """Set a config value, auto-detecting if it should be encrypted based on module.json field type.

    Path format: {module}/tools/{tool_name}/{field_name}  (tool config)
                 {module}/{field_name}                    (module config)
    Returns True if the value was encrypted.
    """
    from .scoped_config import scoped_config_set

    encrypted = is_path_obscure(path)
    scoped_config_set(conn, path, value, scope=scope, scope_id=scope_id, encrypted=encrypted)
    return encrypted
