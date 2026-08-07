from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from agento.framework.config_resolver import (
    ScopedConfigService,
    _db_path,
    _db_path_tool,
    _env_key,
    _env_key_tool,
    read_config_defaults,
)
from agento.framework.config_schema import allowed_scopes as get_allowed_scopes
from agento.framework.config_schema import is_scope_allowed
from agento.framework.scoped_config import Scope


@dataclass
class DashboardData:
    version: str = ""
    python_version: str = ""
    db_connected: bool = False
    module_count: int = 0
    running_jobs: int = 0
    recent_jobs: list[dict] = field(default_factory=list)
    tokens: list[dict] = field(default_factory=list)
    agent_views: list[dict] = field(default_factory=list)


@dataclass
class ModuleSchema:
    name: str
    fields: dict  # field_name -> {type, label, ...}
    tools: dict  # tool_name -> {field_name -> {type, label}}
    module_path: Path | None = None


_ALL_SCOPES: list[str] = [Scope.DEFAULT, Scope.WORKSPACE, Scope.AGENT_VIEW]


@dataclass
class ResolvedField:
    path: str
    field_name: str
    value: str | None
    display_value: str
    source: str  # "env", "db", "db:inherited", "json", "none"
    field_type: str
    label: str
    obscure: bool
    options: list[dict] | None = None  # For select/multiselect: [{"value": ..., "label": ...}]
    editable_at_scope: bool = True
    allowed_scopes: list[str] = field(default_factory=lambda: list(_ALL_SCOPES))
    description: str = ""


def _count_modules() -> int:
    from ..bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR

    count = 0
    for modules_dir in (CORE_MODULES_DIR, USER_MODULES_DIR):
        base = Path(modules_dir)
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if entry.is_dir() and not entry.name.startswith("_") and (entry / "module.json").exists():
                count += 1
    return count


def _ensure_conn(conn) -> None:
    """Reconnect if the DB connection has gone stale."""
    conn.ping(reconnect=True)


def get_dashboard_data(conn) -> DashboardData:
    from ..cli._templates import get_package_version

    data = DashboardData(
        version=get_package_version(),
        python_version=sys.version.split()[0],
    )

    # DB connection check
    if conn is not None:
        try:
            conn.ping(reconnect=True)
            data.db_connected = True
        except Exception:
            data.db_connected = False

    # Module count (filesystem-based, no DB needed)
    with contextlib.suppress(Exception):
        data.module_count = _count_modules()

    if not data.db_connected:
        return data

    # Running jobs
    with contextlib.suppress(Exception), conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM job WHERE status='RUNNING'")
        row = cur.fetchone()
        data.running_jobs = row["cnt"]

    # Recent jobs
    with contextlib.suppress(Exception), conn.cursor() as cur:
        cur.execute(
            "SELECT j.id, j.type, j.status, j.reference_id, j.created_at, j.finished_at, "
            "av.code AS agent_view_code "
            "FROM job j LEFT JOIN agent_view av ON j.agent_view_id = av.id "
            "ORDER BY j.id DESC LIMIT 3"
        )
        data.recent_jobs = cur.fetchall()

    # Tokens
    with contextlib.suppress(Exception):
        from ..agent_manager.token_store import list_tokens

        tokens = list_tokens(conn, enabled_only=False)
        data.tokens = [
            {
                "id": t.id,
                "agent_type": t.agent_type.value,
                "label": t.label,
                "status": t.status.value,
                "error_msg": t.error_msg,
                "used_at": t.used_at,
                "expires_at": t.expires_at,
                "enabled": t.enabled,
            }
            for t in tokens
        ]

    # Agent views
    with contextlib.suppress(Exception):
        from ..workspace import get_active_agent_views

        views = get_active_agent_views(conn)
        data.agent_views = [
            {"id": av.id, "code": av.code, "label": av.label, "workspace_id": av.workspace_id}
            for av in views
        ]

    return data


def get_jobs(
    conn,
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    source: str | None = None,
    agent_view_code: str | None = None,
    strict: bool = False,
) -> list[dict]:
    if conn is None:
        return []

    def _query() -> list[dict]:
        _ensure_conn(conn)
        sql = (
            "SELECT j.id, j.type, j.status, j.source, j.reference_id, j.agent_type, "
            "j.created_at, j.started_at, j.finished_at, j.input_tokens, j.output_tokens, "
            "j.error_class, j.error_message, "
            "av.code AS agent_view_code "
            "FROM job j LEFT JOIN agent_view av ON j.agent_view_id = av.id"
        )
        conditions: list[str] = []
        params: list = []
        if status:
            conditions.append("j.status = %s")
            params.append(status)
        if source:
            conditions.append("j.source = %s")
            params.append(source)
        if agent_view_code:
            conditions.append("av.code = %s")
            params.append(agent_view_code)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY j.id DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    # strict=True lets query errors propagate — callers like `job:list` (an observability command) must
    # NOT have a DB failure silently rendered as "no jobs". The admin TUI keeps the default swallow.
    if strict:
        return _query()
    try:
        return _query()
    except Exception:
        return []


def get_job_detail(conn, job_id: int) -> dict | None:
    if conn is None:
        return None
    try:
        _ensure_conn(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT j.*, av.code AS agent_view_code "
                "FROM job j LEFT JOIN agent_view av ON j.agent_view_id = av.id "
                "WHERE j.id = %s",
                (job_id,),
            )
            return cur.fetchone()
    except Exception:
        return None


def get_tokens_with_usage(conn, *, window_hours: int = 24) -> list[dict]:
    if conn is None:
        return []
    try:
        _ensure_conn(conn)
        from ..agent_manager.token_store import list_tokens
        from ..agent_manager.usage_store import get_usage_summary

        tokens = list_tokens(conn, enabled_only=False)
        results = []
        for t in tokens:
            usage = get_usage_summary(conn, t.id, window_hours)
            pct_free = 100.0
            if t.token_limit > 0:
                pct_free = max(0.0, (1 - usage.total_tokens / t.token_limit) * 100)
            results.append({
                "id": t.id,
                "agent_type": t.agent_type.value,
                "label": t.label,
                "status": t.status.value,
                "error_msg": t.error_msg,
                "used_at": t.used_at,
                "expires_at": t.expires_at,
                "token_limit": t.token_limit,
                "enabled": t.enabled,
                "tokens_used": usage.total_tokens,
                "call_count": usage.call_count,
                "pct_free": round(pct_free, 1),
            })
        return results
    except Exception:
        return []


def get_agents_summary(conn) -> list[dict]:
    if conn is None:
        return []
    try:
        _ensure_conn(conn)
        from ..workspace import get_active_agent_views

        views = get_active_agent_views(conn)

        # Ingress counts
        ingress_counts: dict[int, int] = {}
        with contextlib.suppress(Exception), conn.cursor() as cur:
            cur.execute(
                "SELECT agent_view_id, COUNT(*) AS cnt "
                "FROM ingress_identity WHERE is_active = 1 "
                "GROUP BY agent_view_id"
            )
            for row in cur.fetchall():
                ingress_counts[row["agent_view_id"]] = row["cnt"]

        # Build status
        build_status: dict[int, str] = {}
        with contextlib.suppress(Exception), conn.cursor() as cur:
            cur.execute(
                "SELECT wb.agent_view_id, wb.status "
                "FROM workspace_build wb "
                "INNER JOIN ("
                "  SELECT agent_view_id, MAX(created_at) AS max_created "
                "  FROM workspace_build GROUP BY agent_view_id"
                ") latest ON wb.agent_view_id = latest.agent_view_id "
                "AND wb.created_at = latest.max_created"
            )
            for row in cur.fetchall():
                build_status[row["agent_view_id"]] = row["status"]

        # Workspace labels
        ws_labels: dict[int, str] = {}
        ws_ids = {av.workspace_id for av in views}
        if ws_ids:
            with contextlib.suppress(Exception), conn.cursor() as cur:
                placeholders = ",".join(["%s"] * len(ws_ids))
                cur.execute(
                    f"SELECT id, code FROM workspace WHERE id IN ({placeholders})",
                    tuple(ws_ids),
                )
                for row in cur.fetchall():
                    ws_labels[row["id"]] = row["code"]

        results = []
        for av in views:
            results.append({
                "id": av.id,
                "code": av.code,
                "label": av.label,
                "workspace_code": ws_labels.get(av.workspace_id, ""),
                "ingress_count": ingress_counts.get(av.id, 0),
                "build_status": build_status.get(av.id, "none"),
            })
        return results
    except Exception:
        return []


_module_schema_cache: list[ModuleSchema] | None = None


def get_module_schemas() -> list[ModuleSchema]:
    global _module_schema_cache
    if _module_schema_cache is not None:
        return _module_schema_cache

    from ..bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
    from ..module_loader import scan_modules
    from ..module_status import filter_enabled

    schemas: list[ModuleSchema] = []
    for modules_dir in (CORE_MODULES_DIR, USER_MODULES_DIR):
        if not Path(modules_dir).is_dir():
            continue
        try:
            manifests = filter_enabled(scan_modules(modules_dir))
        except Exception:
            continue
        for m in manifests:
            if not m.config and not m.tools:
                continue
            tool_fields: dict = {}
            for tool in m.tools:
                tool_name = tool.get("name", "")
                fields = tool.get("fields", {})
                if fields:
                    tool_fields[tool_name] = fields
            schemas.append(ModuleSchema(
                name=m.name,
                fields=dict(m.config),
                tools=tool_fields,
                module_path=m.path,
            ))

    _module_schema_cache = schemas
    return schemas


def clear_module_schema_cache() -> None:
    global _module_schema_cache
    _module_schema_cache = None


def get_resolved_fields(conn, module: str, scope: str = Scope.DEFAULT, scope_id: int = 0) -> list[ResolvedField]:
    schemas = get_module_schemas()
    target = None
    for s in schemas:
        if s.name == module:
            target = s
            break
    if target is None:
        return []

    if conn is not None:
        _ensure_conn(conn)

    config_defaults = read_config_defaults(target.module_path) if target.module_path else {}

    # Single resolver owns the ENV -> scoped DB -> config.json fallback-order
    # decision (incl. db:inherited). Value extraction below stays raw — no
    # decryption (obscure secrets show as ****), no type coercion.
    svc = ScopedConfigService(conn, scope, scope_id)

    def _display_source(rv_source: str, inherited: bool) -> str:
        if rv_source == "config.json":
            return "json"
        if rv_source == "db" and inherited:
            return "db:inherited"
        return rv_source

    results: list[ResolvedField] = []
    for field_name, field_schema in target.fields.items():
        field_type = field_schema.get("type", "string")
        label = field_schema.get("label", field_name)
        description = field_schema.get("description", "")
        obscure = field_type == "obscure"
        db_path = _db_path(module, field_name)
        env_key = _env_key(module, field_name)

        rv, inherited = svc.resolve_field_with_source(module, field_name, field_schema, config_defaults)
        source = _display_source(rv.source, inherited)
        if source == "env":
            value = os.environ.get(env_key)
        elif source in ("db", "db:inherited"):
            value = svc.overrides.get(db_path, (None, False))[0]
        elif source == "json":
            value = str(config_defaults[field_name])
        else:
            value = None

        display_value = "****" if obscure and value else (value if value is not None else "")
        editable = is_scope_allowed(field_schema, scope)
        scopes_list = get_allowed_scopes(field_schema)
        if not editable:
            display_value = f"{display_value} [readonly]" if display_value else "[readonly]"
        options = field_schema.get("options") if field_type in ("select", "multiselect") else None
        results.append(ResolvedField(
            path=f"{module}/{field_name}",
            field_name=field_name,
            value=value,
            display_value=display_value,
            source=source,
            field_type=field_type,
            label=label,
            obscure=obscure,
            options=options,
            editable_at_scope=editable,
            allowed_scopes=scopes_list,
            description=description,
        ))

    # Tool fields
    tool_defaults = config_defaults.get("tools", {})
    for tool_name, tool_fields in target.tools.items():
        tool_json = tool_defaults.get(tool_name, {})
        for field_name, field_schema in tool_fields.items():
            field_type = field_schema.get("type", "string")
            label = field_schema.get("label", field_name)
            description = field_schema.get("description", "")
            obscure = field_type == "obscure"
            db_path = _db_path_tool(module, tool_name, field_name)
            env_key = _env_key_tool(module, tool_name, field_name)

            rv, inherited = svc.resolve_tool_field_with_source(
                module, tool_name, field_name, field_schema, config_defaults
            )
            source = _display_source(rv.source, inherited)
            if source == "env":
                value = os.environ.get(env_key)
            elif source in ("db", "db:inherited"):
                value = svc.overrides.get(db_path, (None, False))[0]
            elif source == "json":
                value = str(tool_json[field_name])
            else:
                value = None

            display_value = "****" if obscure and value else (value if value is not None else "")
            editable = is_scope_allowed(field_schema, scope)
            scopes_list = get_allowed_scopes(field_schema)
            if not editable:
                display_value = f"{display_value} [readonly]" if display_value else "[readonly]"
            options = field_schema.get("options") if field_type in ("select", "multiselect") else None
            results.append(ResolvedField(
                path=f"{module}/tools/{tool_name}/{field_name}",
                field_name=field_name,
                value=value,
                display_value=display_value,
                source=source,
                field_type=field_type,
                label=label,
                obscure=obscure,
                options=options,
                editable_at_scope=editable,
                allowed_scopes=scopes_list,
                description=description,
            ))

    return results


def get_workspaces(conn) -> list[dict]:
    if conn is None:
        return []
    try:
        _ensure_conn(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT id, code, label, is_active FROM workspace ORDER BY id")
            return cur.fetchall()
    except Exception:
        return []


def get_agent_views(conn, workspace_id: int | None = None) -> list[dict]:
    if conn is None:
        return []
    try:
        _ensure_conn(conn)
        sql = "SELECT id, code, label, workspace_id, is_active FROM agent_view"
        params: list = []
        if workspace_id is not None:
            sql += " WHERE workspace_id = %s"
            params.append(workspace_id)
        sql += " ORDER BY id"
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception:
        return []


def set_config_value(conn, path: str, value: str, scope: str = Scope.DEFAULT, scope_id: int = 0) -> None:
    from ..core_config import config_set_auto_encrypt

    _ensure_conn(conn)
    config_set_auto_encrypt(conn, path, value, scope=scope, scope_id=scope_id)
    conn.commit()


def delete_config_override(conn, path: str, scope: str = Scope.DEFAULT, scope_id: int = 0) -> bool:
    from ..core_config import config_delete

    _ensure_conn(conn)
    result = config_delete(conn, path, scope=scope, scope_id=scope_id)
    conn.commit()
    return result


# --- Tool/skill enablement (Access screen) ------------------------------------
#
# Opt-in model: a tool/skill is enabled only when its resolved is_enabled value
# is '1'. These helpers feed the Access screen's checkbox lists; the checkbox
# state is the *resolved* value at the selected scope (so inherited '1's show as
# checked), and `explicit_here` tells whether a row is set at this very scope.


@dataclass
class EnablementItem:
    name: str
    path: str  # core_config_data path, e.g. tools/<name>/is_enabled
    enabled: bool  # the tool's OWN resolved value at the selected scope == '1'
    explicit_here: bool  # a row exists at this exact (scope, scope_id)
    # Nearest ancestor in the manifest `requires` chain that is not enabled, else None. The
    # tool is therefore unavailable at runtime even when `enabled` is True. Optional so
    # get_skill_states (skills have no masters) is unaffected.
    blocked_by: str | None = None


def get_all_skill_names(conn) -> list[str]:
    """All synced skills, alphabetical. Empty if the skill module/table is absent."""
    if conn is None:
        return []
    try:
        _ensure_conn(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM skill_registry ORDER BY name")
            return [row["name"] for row in cur.fetchall()]
    except Exception:
        return []


def _scan_tools_by_toolset() -> list[tuple[str, list[str]]]:
    """All registered tools grouped by toolset, toolsets and names both sorted.

    A tool's group is its ``toolset`` field in module.json (declared next to the
    tool), falling back to the module name when absent. Tools from different
    modules that share a ``toolset`` are grouped together. Enumerates manifests
    directly (like ``tool:list``) so field-less tools are included.
    """
    from ..bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
    from ..module_loader import scan_modules
    from ..module_status import filter_enabled

    groups: dict[str, list[str]] = {}
    for modules_dir in (CORE_MODULES_DIR, USER_MODULES_DIR):
        if not Path(modules_dir).is_dir():
            continue
        try:
            manifests = filter_enabled(scan_modules(modules_dir))
        except Exception:
            continue
        for m in manifests:
            for tool in m.tools:
                name = tool.get("name")
                if not name:
                    continue
                toolset = tool.get("toolset") or m.name
                groups.setdefault(toolset, []).append(name)
    return [(toolset, sorted(names)) for toolset, names in sorted(groups.items())]


def get_tool_states(conn, scope: str = Scope.DEFAULT, scope_id: int = 0) -> list[tuple[str, list[EnablementItem]]]:
    """Resolved tool enablement grouped by toolset, for the Tools screen.

    Resolution routes through the single config service (ScopedConfigService).
    Tool names are snake_case, so ``.get()`` is dash-safe and honors a config.json
    first-class default and ENV (module-agnostic ``tools/<name>/is_enabled``
    defaults included, via the literal-path fallback in ``ScopedConfigService``).

    ``enabled`` is the tool's OWN resolved value — that is what a checkbox toggle
    writes. ``blocked_by`` carries effectiveness: a tool whose declared ``requires``
    master is off is denied at runtime even when its own value is ``1``.
    """
    if conn is None:
        return []

    from ..config_resolver import ScopedConfigService
    from ..tool_enablement import blocked_by, scan_tool_requires

    _ensure_conn(conn)
    svc = ScopedConfigService(conn, scope, scope_id)
    requires = scan_tool_requires()

    def _resolve(tool_name: str) -> bool:
        return svc.get(f"tools/{tool_name}/is_enabled") == "1"

    def _item(name: str) -> EnablementItem:
        path = f"tools/{name}/is_enabled"
        return EnablementItem(
            name, path, _resolve(name), svc.is_set_at_scope(path),
            blocked_by=blocked_by(name, requires, _resolve),
        )

    return [(toolset, [_item(n) for n in names]) for toolset, names in _scan_tools_by_toolset()]


def get_skill_states(conn, scope: str = Scope.DEFAULT, scope_id: int = 0) -> list[EnablementItem]:
    """Resolved skill enablement (alphabetical) for the Skills screen.

    Skill names may contain dashes (e.g. ``git-workflow``), which the service's
    ``.get()`` path-normalizes; read the merged ``.overrides`` to stay dash-exact.
    """
    if conn is None:
        return []

    from ..config_resolver import ScopedConfigService

    _ensure_conn(conn)
    svc = ScopedConfigService(conn, scope, scope_id)

    def _item(name: str) -> EnablementItem:
        path = f"skill/{name}/is_enabled"
        entry = svc.overrides.get(path)
        return EnablementItem(name, path, entry is not None and entry[0] == "1", svc.is_set_at_scope(path))

    return [_item(s) for s in get_all_skill_names(conn)]


def do_reset_token_error(conn, token_id: int) -> bool:
    from ..agent_manager.token_store import clear_token_error

    _ensure_conn(conn)
    result = clear_token_error(conn, token_id)
    conn.commit()
    return result


def do_mark_token_error(conn, token_id: int, message: str) -> bool:
    from ..agent_manager.token_store import mark_token_error

    _ensure_conn(conn)
    result = mark_token_error(conn, token_id, message)
    conn.commit()
    return result


def do_deregister_token(conn, token_id: int) -> bool:
    from ..agent_manager.token_store import deregister_token

    _ensure_conn(conn)
    result = deregister_token(conn, token_id)
    conn.commit()
    return result
