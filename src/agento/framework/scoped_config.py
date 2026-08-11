"""Scope constants + DB-override loading for scoped config resolution.

The ENV -> DB -> config.json fallback itself lives in a single place —
``ScopedConfigService`` in ``config_resolver.py``. This module only provides:

  * ``Scope`` constants (default / workspace / agent_view),
  * ``load_scoped_db_overrides`` / ``build_scoped_overrides`` — load and merge
    ``core_config_data`` rows across the 3-tier scope chain (agent_view ->
    workspace -> default), the DB tier the service resolves against,
  * ``scoped_config_set`` — write a scoped value.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class Scope:
    """Magento-style scope constants for 3-tier config resolution."""

    DEFAULT = "default"
    WORKSPACE = "workspace"
    AGENT_VIEW = "agent_view"


def load_scoped_db_overrides(
    conn,
    scope: str = Scope.DEFAULT,
    scope_id: int = 0,
) -> dict[str, tuple[str, bool]]:
    """Load core_config_data rows for a specific (scope, scope_id).

    Returns {path: (value, encrypted)}.
    """
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT path, value, encrypted FROM core_config_data "
                "WHERE scope = %s AND scope_id = %s",
                (scope, scope_id),
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
        logger.warning("Failed to load scoped overrides (%s/%s)", scope, scope_id, exc_info=True)
        return {}


def build_scoped_overrides(
    conn,
    agent_view_id: int | None = None,
    workspace_id: int | None = None,
) -> dict[str, tuple[str, bool]]:
    """Build merged DB overrides with 3-tier fallback: agent_view -> workspace -> global.

    Later tiers (more specific) override earlier ones for the same path.
    """
    # Start with global
    merged = load_scoped_db_overrides(conn, Scope.DEFAULT, 0)

    # Layer workspace overrides
    if workspace_id is not None:
        ws_overrides = load_scoped_db_overrides(conn, Scope.WORKSPACE, workspace_id)
        merged.update(ws_overrides)

    # Layer agent_view overrides (most specific)
    if agent_view_id is not None:
        av_overrides = load_scoped_db_overrides(conn, Scope.AGENT_VIEW, agent_view_id)
        merged.update(av_overrides)

    return merged


def scoped_config_set(
    conn,
    path: str,
    value: str,
    *,
    scope: str = Scope.DEFAULT,
    scope_id: int = 0,
    encrypted: bool = False,
) -> None:
    """Set a scoped config value (INSERT or UPDATE)."""
    from .encryptor import get_encryptor

    stored_value = get_encryptor().encrypt(value) if encrypted else value
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO core_config_data (scope, scope_id, path, value, encrypted)
               VALUES (%s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE value = VALUES(value), encrypted = VALUES(encrypted)""",
            (scope, scope_id, path, stored_value, int(encrypted)),
        )


# Origin ranks for a config value, highest wins. `build_scoped_overrides` flattens the
# three DB scopes into one dict, which is fine for plain reads but loses WHERE a value
# came from — and the legacy harness/provider fallback has to compare exactly that
# (a view-level harness must beat a global legacy provider).
ORIGIN_ENV = 40
ORIGIN_DB_AGENT_VIEW = 30
ORIGIN_DB_WORKSPACE = 20
ORIGIN_DB_DEFAULT = 10
ORIGIN_CONFIG_JSON = 0
ORIGIN_ABSENT = -1


def resolve_with_origin(
    conn,
    path: str,
    *,
    agent_view_id: int | None = None,
    workspace_id: int | None = None,
    config_json_value: str | None = None,
) -> tuple[str | None, int]:
    """Resolve one config path to ``(value, origin_rank)``.

    Deliberately narrow: only the two paths whose relative precedence decides the
    legacy harness/provider fallback need this, so it does per-scope lookups instead
    of rewriting the shared resolver.
    """
    import os

    from .config_resolver import path_to_env_key

    env_value = os.environ.get(path_to_env_key(path))
    if env_value is not None:
        return env_value, ORIGIN_ENV

    for scope, scope_id, rank in (
        (Scope.AGENT_VIEW, agent_view_id, ORIGIN_DB_AGENT_VIEW),
        (Scope.WORKSPACE, workspace_id, ORIGIN_DB_WORKSPACE),
        (Scope.DEFAULT, 0, ORIGIN_DB_DEFAULT),
    ):
        if scope_id is None:
            continue
        rows = load_scoped_db_overrides(conn, scope, scope_id)
        entry = rows.get(path)
        if entry is not None and entry[0] is not None:
            return entry[0], rank

    if config_json_value is not None:
        return config_json_value, ORIGIN_CONFIG_JSON
    return None, ORIGIN_ABSENT
