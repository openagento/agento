"""Module config resolution with 3-level fallback.

Mirrors the Node.js config-loader.js logic:
  1. ENV: CONFIG__{MODULE}__{FIELD}  (uppercase, hyphens -> underscores)
  2. DB:  core_config_data at path {module}/{field}
  3. config.json defaults in module directory

For tool fields the path includes "tools":
  ENV: CONFIG__{MODULE}__TOOLS__{TOOL}__{FIELD}
  DB:  {module}/tools/{tool}/{field}
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .encryptor import get_encryptor

logger = logging.getLogger(__name__)


@dataclass
class ResolvedValue:
    """A config value with its resolution source."""

    value: Any
    source: str  # "env", "db", "config.json", or "none"


def load_db_overrides(conn) -> dict[str, tuple[str, bool]]:
    """Batch-load all core_config_data rows into {path: (value, encrypted)}.

    Returns empty dict if conn is None or query fails.
    """
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT path, value, encrypted FROM core_config_data "
                "WHERE scope = 'default' AND scope_id = 0"
            )
            rows = cur.fetchall()
        result = {}
        for row in rows:
            if isinstance(row, dict):
                result[row["path"]] = (row["value"], bool(row["encrypted"]))
            else:
                result[row[0]] = (row[1], bool(row[2]))
        return result
    except Exception:
        logger.warning("Failed to load core_config_data overrides", exc_info=True)
        return {}


_config_defaults_cache: dict[Path, tuple[float, dict]] = {}


def read_config_defaults(module_path: Path) -> dict:
    """Read config.json from a module directory. Returns empty dict if absent.

    Cached by mtime: re-reads only when the file changes (so ``app/code``
    hot-reload still applies). Callers treat the returned dict as read-only.
    """
    config_path = module_path / "config.json"
    try:
        mtime = config_path.stat().st_mtime
    except OSError:
        return {}

    cached = _config_defaults_cache.get(config_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        data = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", config_path, exc)
        return {}

    _config_defaults_cache[config_path] = (mtime, data)
    return data


def _resolve_literal_config_json(path: str) -> str | None:
    """Resolve a module-agnostic ``config.json`` key by its LITERAL path.

    Mirrors the toolbox's ``loadConfigDefaults()``, which merges every module's
    ``config.json`` with ``Object.assign`` and looks a path up verbatim — so the
    LAST declaring module wins, and this must too. Needed for keys whose first
    segment is not a module name, notably ``tools/<name>/is_enabled``, which
    ``_parse_config_path`` reads as module ``tools`` and can never resolve.
    Without this, ``tool:list`` and the admin Tools screen report a live
    first-class tool as disabled.
    """
    from .bootstrap import get_manifests

    found: str | None = None
    for manifest in get_manifests():
        defaults = read_config_defaults(manifest.path)
        if defaults and path in defaults:
            val = defaults[path]
            found = str(val) if val is not None else None
    return found


def _coerce_type(value: str, field_type: str) -> Any:
    """Coerce a string value to the declared field type."""
    if field_type == "integer":
        return int(value)
    if field_type == "boolean":
        return value.lower() in ("1", "true", "yes")
    if field_type == "json":
        return json.loads(value)
    # string, obscure -> return as-is
    return value


def _env_key(module_name: str, field_name: str) -> str:
    """Build ENV var name: CONFIG__{MODULE}__{FIELD}.

    Uppercased, with ``-`` → ``_`` and ``/`` → ``__`` so slash-keyed schema
    fields (e.g. ``identity/ssh_private_key``) produce valid POSIX env names.
    """
    return f"CONFIG__{module_name}__{field_name}".upper().replace("-", "_").replace("/", "__")


def _env_key_tool(module_name: str, tool_name: str, field_name: str) -> str:
    """Build ENV var name for tool field."""
    return (
        f"CONFIG__{module_name}__TOOLS__{tool_name}__{field_name}"
        .upper().replace("-", "_").replace("/", "__")
    )


def path_to_env_key(path: str) -> str:
    """Map a DB config path to its CONFIG__* env-var name.

    ``agent_view/provider`` -> ``CONFIG__AGENT_VIEW__PROVIDER``;
    slash-keyed fields (e.g. ``agent_view/identity/ssh_private_key``) and tool
    paths (``mod/tools/tool/field``) are handled via ``_parse_config_path``.
    """
    from .core_config import _parse_config_path

    parsed = _parse_config_path(path)
    if parsed is None:
        module_name = path.partition("/")[0]
        return f"CONFIG__{module_name}".upper().replace("-", "_")
    module_name, tool_name, field_name = parsed
    if tool_name is not None:
        return _env_key_tool(module_name, tool_name, field_name)
    return _env_key(module_name, field_name)


def env_key_to_path(env_key: str) -> str:
    """Inverse of ``path_to_env_key``: ``CONFIG__*`` env var name -> config path.

    ``CONFIG__AGENT_VIEW__CODEX__APPROVAL_MODE`` -> ``agent_view/codex/approval_mode``.
    Generic — no module/agent-specific knowledge. Path segments must not contain
    hyphens (``path_to_env_key`` folds ``-`` and the ``/`` separator differently,
    so only the ``__``->``/`` mapping is reversible); no config key uses hyphens.
    """
    return env_key.removeprefix("CONFIG__").lower().replace("__", "/")


def _db_path(module_name: str, field_name: str) -> str:
    """Build DB path: {module}/{field}, hyphens -> underscores."""
    return f"{module_name}/{field_name}".replace("-", "_")


def _db_path_tool(module_name: str, tool_name: str, field_name: str) -> str:
    """Build DB path for tool field."""
    return f"{module_name}/tools/{tool_name}/{field_name}".replace("-", "_")


class DecryptError(Exception):
    """A DB override exists but could not be decrypted.

    Carries the config PATH only — never the ciphertext and never the partially
    decrypted value, both of which would end up in a log line or a traceback.
    """

    def __init__(self, path: str):
        super().__init__(f"could not decrypt the stored value for {path!r}")
        self.path = path


def _resolve_from_db(
    db_path: str,
    db_overrides: dict[str, tuple[str, bool]],
    strict: bool = False,
) -> tuple[str | None, bool]:
    """Look up a DB override. Returns (value_or_None, found).

    ``strict`` raises ``DecryptError`` instead of reporting "not found" when a
    row exists and cannot be decrypted. A caller that is *diagnosing* stored
    config needs the two cases distinguished; a caller that is merely *reading*
    config wants the lenient fallback, which is why this is opt-in.
    """
    override = db_overrides.get(db_path)
    if override is None:
        return None, False
    value, encrypted = override
    if encrypted:
        try:
            return get_encryptor().decrypt(value), True
        except Exception:
            logger.error("Failed to decrypt %s", db_path, exc_info=True)
            if strict:
                raise DecryptError(db_path) from None
            return None, False
    return value, True


def resolve_field(
    module_name: str,
    field_name: str,
    field_schema: dict,
    config_defaults: dict,
    db_overrides: dict[str, tuple[str, bool]],
) -> ResolvedValue:
    """Resolve a single module config field using 3-level fallback."""
    field_type = field_schema.get("type", "string")

    # 1. ENV var (highest priority)
    env_val = os.environ.get(_env_key(module_name, field_name))
    if env_val is not None:
        return ResolvedValue(value=_coerce_type(env_val, field_type), source="env")

    # 2. DB override
    db_val, found = _resolve_from_db(
        _db_path(module_name, field_name), db_overrides
    )
    if found and db_val is not None:
        return ResolvedValue(value=_coerce_type(db_val, field_type), source="db")

    # 3. config.json default
    cfg_val = config_defaults.get(field_name)
    if cfg_val is not None:
        return ResolvedValue(value=cfg_val, source="config.json")

    return ResolvedValue(value=None, source="none")


def resolve_tool_field(
    module_name: str,
    tool_name: str,
    field_name: str,
    field_schema: dict,
    config_defaults: dict,
    db_overrides: dict[str, tuple[str, bool]],
) -> ResolvedValue:
    """Resolve a single tool config field using 3-level fallback.

    Matches Node.js config-loader.js resolveField() exactly.
    """
    field_type = field_schema.get("type", "string")

    # 1. ENV var
    env_val = os.environ.get(_env_key_tool(module_name, tool_name, field_name))
    if env_val is not None:
        return ResolvedValue(value=_coerce_type(env_val, field_type), source="env")

    # 2. DB override
    db_val, found = _resolve_from_db(
        _db_path_tool(module_name, tool_name, field_name), db_overrides
    )
    if found and db_val is not None:
        return ResolvedValue(value=_coerce_type(db_val, field_type), source="db")

    # 3. config.json default (nested under tools/tool_name/field_name)
    cfg_val = config_defaults.get("tools", {}).get(tool_name, {}).get(field_name)
    if cfg_val is not None:
        return ResolvedValue(value=cfg_val, source="config.json")

    return ResolvedValue(value=None, source="none")


def get_timezone(
    db_overrides: dict[str, tuple[str, bool]],
    config_defaults: dict,
) -> ZoneInfo:
    """Resolve the timezone from core/timezone config path."""
    schema = {"type": "string"}
    rv = resolve_field("core", "timezone", schema, config_defaults, db_overrides)
    tz_name = rv.value if rv.value else "UTC"
    return ZoneInfo(tz_name)


def resolve_module_config(
    manifest,
    config_defaults: dict,
    db_overrides: dict[str, tuple[str, bool]],
) -> dict[str, Any]:
    """Resolve all config fields declared in a module manifest.

    Returns {field_name: resolved_value}.
    """
    result = {}
    for field_name, field_schema in manifest.config.items():
        resolved = resolve_field(
            manifest.name, field_name, field_schema, config_defaults, db_overrides
        )
        result[field_name] = resolved.value
    return result


def resolve_module_config_with_sources(
    manifest,
    config_defaults: dict,
    db_overrides: dict[str, tuple[str, bool]],
) -> dict[str, ResolvedValue]:
    """Resolve all config fields with source information (for config:list display)."""
    result = {}
    for field_name, field_schema in manifest.config.items():
        result[field_name] = resolve_field(
            manifest.name, field_name, field_schema, config_defaults, db_overrides
        )
    return result


class ScopedConfigService:
    """The single ENV -> DB -> config.json config resolver.

    One instance per (scope, scope_id). DB overrides are pre-merged once at
    construction via ``build_scoped_overrides`` (agent_view -> workspace ->
    default), so every read shares the same scope chain. ENV always wins;
    config.json is the final fallback.

    Use ``.get(path)`` for raw-string reads (runtime provider/model, tool
    flags), ``.get_module(name)`` for a fully-resolved+coerced module config,
    and ``.resolve_field_with_source(...)`` for display (admin/CLI). ``.overrides``
    exposes the raw merged dict for dict-indexing consumers.
    """

    def __init__(self, conn, scope: str = "default", scope_id: int = 0, workspace_id: int | None = None):
        """Build a resolver for a (scope, scope_id).

        When ``scope`` is agent_view, pass ``workspace_id`` if already known to
        skip the ``agent_view -> workspace`` lookup query.
        """
        from .scoped_config import (
            Scope,
            build_scoped_overrides,
            load_scoped_db_overrides,
        )

        self._conn = conn
        self._scope = scope
        self._scope_id = scope_id

        agent_view_id = None
        if scope == Scope.AGENT_VIEW:
            agent_view_id = scope_id
            if workspace_id is None:
                workspace_id = self._resolve_workspace_id(conn, scope_id)
        elif scope == Scope.WORKSPACE:
            workspace_id = scope_id

        self._overrides = build_scoped_overrides(
            conn, agent_view_id=agent_view_id, workspace_id=workspace_id
        )
        # Rows set at exactly this (scope, scope_id) — for db:inherited detection.
        self._scope_overrides = load_scoped_db_overrides(conn, scope, scope_id)

    @staticmethod
    def _resolve_workspace_id(conn, agent_view_id: int) -> int | None:
        if conn is None:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT workspace_id FROM agent_view WHERE id = %s", (agent_view_id,)
                )
                row = cur.fetchone()
                if row:
                    return row["workspace_id"] if isinstance(row, dict) else row[0]
        except Exception:
            logger.warning("Failed to resolve workspace_id for agent_view_id=%s", agent_view_id)
        return None

    @property
    def overrides(self) -> dict[str, tuple[str, bool]]:
        """The raw merged DB overrides ({path: (value, encrypted)})."""
        return self._overrides

    def get(self, path: str, *, strict: bool = False) -> str | None:
        """Resolve a raw string value: ENV -> merged DB -> config.json (no type coercion).

        ``strict=True`` raises ``DecryptError`` when a DB row for ``path`` exists
        and cannot be decrypted, instead of falling through to ``config.json``.
        Use it wherever the answer "not set" would be a misdiagnosis.
        """
        env_val = os.environ.get(path_to_env_key(path))
        if env_val is not None:
            return env_val

        db_val, found = _resolve_from_db(
            path.replace("-", "_"), self._overrides, strict=strict
        )
        if found and db_val is not None:
            return db_val

        return self._resolve_config_json(path)

    def is_set_at_scope(self, path: str) -> bool:
        """True if a row for ``path`` exists at exactly this (scope, scope_id).

        Used to distinguish a locally-set value from one inherited up the scope
        chain. Matches DB keys verbatim (no dash normalization), so it is safe for
        paths whose segments contain dashes (e.g. ``skill/git-workflow/is_enabled``).
        """
        return path in self._scope_overrides

    def resolve_all(self) -> dict[str, str]:
        """The full effective config, each path resolved ENV -> DB -> config.json.

        Keys = the union of DB override keys (this scope chain), ``CONFIG__*`` env
        keys (reverse-mapped), and every declared module config field (so a
        config.json-only default with no DB/ENV row is still surfaced). Generic —
        no module/agent-specific paths. Tool-field config.json-only defaults are
        out of scope (toolbox-side, never materialized); tool DB/ENV overrides are
        still included as keys.
        """
        from .bootstrap import get_manifests

        paths = set(self._overrides)
        paths |= {
            env_key_to_path(k) for k in os.environ if k.startswith("CONFIG__")
        }
        for m in get_manifests():
            paths.update(f"{m.name}/{field}" for field in m.config)
        return {p: v for p in paths if (v := self.get(p)) is not None}

    def get_module(self, module_name: str):
        """Resolve a module's full config (coerced); typed dataclass if declared."""
        from .bootstrap import get_manifests
        from .module_loader import import_class

        manifest = next((m for m in get_manifests() if m.name == module_name), None)
        if manifest is None:
            return None

        config_defaults = read_config_defaults(manifest.path)
        resolved = {
            field_name: resolve_field(
                module_name, field_name, field_schema, config_defaults, self._overrides
            ).value
            for field_name, field_schema in manifest.config.items()
        }

        config_class_path = manifest.provides.get("config_class")
        if config_class_path:
            try:
                cls = import_class(manifest.path, config_class_path)
                return cls.from_dict(resolved)
            except Exception:
                logger.exception(
                    "Failed to load config_class %r from module %s, using dict",
                    config_class_path, module_name,
                )
        return resolved

    def resolve_field_with_source(
        self, module_name: str, field_name: str, field_schema: dict, config_defaults: dict
    ) -> tuple[ResolvedValue, bool]:
        """Resolve a module field, returning (ResolvedValue, inherited).

        ``inherited`` is True when the value came from the DB but was set at a
        parent scope, not the requested scope.
        """
        rv = resolve_field(module_name, field_name, field_schema, config_defaults, self._overrides)
        inherited = self._is_inherited(rv, _db_path(module_name, field_name))
        return rv, inherited

    def resolve_tool_field_with_source(
        self,
        module_name: str,
        tool_name: str,
        field_name: str,
        field_schema: dict,
        config_defaults: dict,
    ) -> tuple[ResolvedValue, bool]:
        """Tool-field variant of ``resolve_field_with_source``."""
        rv = resolve_tool_field(
            module_name, tool_name, field_name, field_schema, config_defaults, self._overrides
        )
        inherited = self._is_inherited(rv, _db_path_tool(module_name, tool_name, field_name))
        return rv, inherited

    def _is_inherited(self, rv: ResolvedValue, db_path: str) -> bool:
        from .scoped_config import Scope

        return (
            rv.source == "db"
            and self._scope != Scope.DEFAULT
            and db_path not in self._scope_overrides
        )

    def _resolve_config_json(self, path: str) -> str | None:
        val = self._resolve_module_config_json(path)
        return val if val is not None else _resolve_literal_config_json(path)

    def _resolve_module_config_json(self, path: str) -> str | None:
        from .bootstrap import get_manifests
        from .core_config import _parse_config_path

        parsed = _parse_config_path(path)
        if parsed is None:
            return None
        module_name, tool_name, field_name = parsed

        module_path = next(
            (m.path for m in get_manifests() if m.name == module_name), None
        )
        if module_path is None:
            return None

        defaults = read_config_defaults(module_path)
        if not defaults:
            return None

        if tool_name is not None:
            val = defaults.get("tools", {}).get(tool_name, {}).get(field_name)
        else:
            val = defaults.get(field_name)
        return str(val) if val is not None else None
