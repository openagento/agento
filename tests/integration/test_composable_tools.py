"""Integration tests: composable_tools — tool enable/disable via scoped config.

Uses real MySQL. Tests that tool:enable/disable writes config to DB
and that the scoped config chain (agent_view → workspace → global) resolves correctly.
"""
from __future__ import annotations

from .conftest import _test_connection


def _insert_workspace(code: str = "acme") -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO workspace (code, label) VALUES (%s, %s)", (code, code))
            return cur.lastrowid
    finally:
        conn.close()


def _insert_agent_view(workspace_id: int, code: str = "developer") -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, %s, %s)",
                (workspace_id, code, code),
            )
            return cur.lastrowid
    finally:
        conn.close()


def _set_config(scope: str, scope_id: int, path: str, value: str) -> None:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO core_config_data (scope, scope_id, path, value, encrypted)
                   VALUES (%s, %s, %s, %s, 0)
                   ON DUPLICATE KEY UPDATE value = VALUES(value)""",
                (scope, scope_id, path, value),
            )
    finally:
        conn.close()


def _get_config(scope: str, scope_id: int, path: str) -> str | None:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM core_config_data WHERE scope=%s AND scope_id=%s AND path=%s",
                (scope, scope_id, path),
            )
            row = cur.fetchone()
            return row["value"] if row else None
    finally:
        conn.close()


def _cleanup():
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute("DELETE FROM agent_view")
            cur.execute("DELETE FROM workspace")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    finally:
        conn.close()


class TestToolEnableDisableInDB:
    """tool:enable/disable writes correct scoped config to core_config_data."""

    def setup_method(self):
        _cleanup()

    def teardown_method(self):
        _cleanup()

    def test_disable_tool_globally(self):
        from agento.framework.scoped_config import scoped_config_set

        conn = _test_connection(autocommit=True)
        try:
            scoped_config_set(conn, "tools/jira_search/is_enabled", "0", scope="default", scope_id=0)
        finally:
            conn.close()

        assert _get_config("default", 0, "tools/jira_search/is_enabled") == "0"

    def test_enable_tool_per_agent_view(self):
        from agento.framework.scoped_config import scoped_config_set

        ws_id = _insert_workspace()
        av_id = _insert_agent_view(ws_id)

        conn = _test_connection(autocommit=True)
        try:
            # Disable globally
            scoped_config_set(conn, "tools/jira_search/is_enabled", "0", scope="default", scope_id=0)
            # Enable for specific agent_view
            scoped_config_set(conn, "tools/jira_search/is_enabled", "1", scope="agent_view", scope_id=av_id)
        finally:
            conn.close()

        assert _get_config("default", 0, "tools/jira_search/is_enabled") == "0"
        assert _get_config("agent_view", av_id, "tools/jira_search/is_enabled") == "1"

    def test_scoped_override_resolution(self):
        """agent_view override takes precedence over global."""
        from agento.framework.scoped_config import build_scoped_overrides, scoped_config_set

        ws_id = _insert_workspace()
        av_id = _insert_agent_view(ws_id)

        conn = _test_connection(autocommit=True)
        try:
            scoped_config_set(conn, "tools/browser/is_enabled", "1", scope="default", scope_id=0)
            scoped_config_set(conn, "tools/browser/is_enabled", "0", scope="agent_view", scope_id=av_id)

            overrides = build_scoped_overrides(conn, agent_view_id=av_id, workspace_id=ws_id)
        finally:
            conn.close()

        value, _encrypted = overrides["tools/browser/is_enabled"]
        assert value == "0", "agent_view scope should override global"

    def test_workspace_scope_inherits_to_agent_view(self):
        """Workspace-level disable applies to all agent_views in that workspace."""
        from agento.framework.scoped_config import build_scoped_overrides, scoped_config_set

        ws_id = _insert_workspace()
        av_id = _insert_agent_view(ws_id)

        conn = _test_connection(autocommit=True)
        try:
            scoped_config_set(conn, "tools/email/is_enabled", "0", scope="workspace", scope_id=ws_id)

            overrides = build_scoped_overrides(conn, agent_view_id=av_id, workspace_id=ws_id)
        finally:
            conn.close()

        value, _encrypted = overrides["tools/email/is_enabled"]
        assert value == "0"

    def test_agent_view_overrides_workspace(self):
        """Agent_view-level enable overrides workspace-level disable."""
        from agento.framework.scoped_config import build_scoped_overrides, scoped_config_set

        ws_id = _insert_workspace()
        av_id = _insert_agent_view(ws_id)

        conn = _test_connection(autocommit=True)
        try:
            scoped_config_set(conn, "tools/email/is_enabled", "0", scope="workspace", scope_id=ws_id)
            scoped_config_set(conn, "tools/email/is_enabled", "1", scope="agent_view", scope_id=av_id)

            overrides = build_scoped_overrides(conn, agent_view_id=av_id, workspace_id=ws_id)
        finally:
            conn.close()

        value, _encrypted = overrides["tools/email/is_enabled"]
        assert value == "1"

    def test_absent_config_means_disabled(self):
        """Opt-in: when no config row exists for a tool, it is disabled.

        The merged overrides simply omit the path; the gate (isToolEnabled)
        treats a missing entry as disabled.
        """
        from agento.framework.scoped_config import build_scoped_overrides

        ws_id = _insert_workspace()
        av_id = _insert_agent_view(ws_id)

        conn = _test_connection(autocommit=True)
        try:
            overrides = build_scoped_overrides(conn, agent_view_id=av_id, workspace_id=ws_id)
        finally:
            conn.close()

        # No row -> path absent from the merged map -> tool disabled under opt-in.
        assert "tools/jira_search/is_enabled" not in overrides
