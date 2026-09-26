"""Tests for scoped config resolution (agent_view -> workspace -> global fallback)."""
from __future__ import annotations

from unittest.mock import MagicMock

from agento.framework.scoped_config import (
    Scope,
    build_scoped_overrides,
    load_scoped_db_overrides,
    scoped_config_set,
)


def _make_conn(rows):
    """Create a mock DB connection that returns the given rows."""
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


class TestLoadScopedDbOverrides:
    def test_returns_empty_for_none_conn(self):
        assert load_scoped_db_overrides(None) == {}

    def test_loads_dict_rows(self):
        rows = [
            {"path": "jira/token", "value": "abc", "encrypted": 0},
            {"path": "jira/url", "value": "https://jira.test", "encrypted": 0},
        ]
        result = load_scoped_db_overrides(_make_conn(rows), "default", 0)
        assert result == {
            "jira/token": ("abc", False),
            "jira/url": ("https://jira.test", False),
        }

    def test_loads_tuple_rows(self):
        rows = [("jira/token", "abc", 0)]
        conn = _make_conn(rows)
        result = load_scoped_db_overrides(conn, "workspace", 1)
        assert result == {"jira/token": ("abc", False)}

    def test_handles_db_error(self):
        conn = MagicMock()
        conn.cursor.side_effect = Exception("DB down")
        assert load_scoped_db_overrides(conn) == {}


class TestBuildScopedOverrides:
    def _conn_returning(self, scope_data):
        """Create conn that returns different rows based on (scope, scope_id) args."""
        def cursor_factory():
            cur = MagicMock()
            # Track the execute args to return correct data
            cur._scope_key = None

            def execute(sql, params):
                cur._scope_key = (params[0], params[1])

            def fetchall():
                key = cur._scope_key
                rows = scope_data.get(key, [])
                return [{"path": p, "value": v, "encrypted": 0} for p, v in rows]

            cur.execute = execute
            cur.fetchall = fetchall
            return cur

        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(side_effect=cursor_factory)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn

    def test_global_only(self):
        rows = [{"path": "jira/url", "value": "https://global.test", "encrypted": 0}]
        conn = _make_conn(rows)
        result = build_scoped_overrides(conn)
        assert result["jira/url"] == ("https://global.test", False)

    def test_workspace_overrides_global(self):
        # Mock: return different data based on scope
        global_rows = [{"path": "agent_view/model", "value": "sonnet", "encrypted": 0}]
        ws_rows = [{"path": "agent_view/model", "value": "opus", "encrypted": 0}]

        call_count = [0]

        def make_cursor():
            cur = MagicMock()
            idx = call_count[0]
            call_count[0] += 1
            if idx == 0:
                cur.fetchall.return_value = global_rows
            else:
                cur.fetchall.return_value = ws_rows
            return cur

        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(side_effect=make_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = build_scoped_overrides(conn, workspace_id=1)
        assert result["agent_view/model"] == ("opus", False)

    def test_agent_view_overrides_workspace(self):
        call_count = [0]

        def make_cursor():
            cur = MagicMock()
            idx = call_count[0]
            call_count[0] += 1
            if idx == 0:
                # global
                cur.fetchall.return_value = [
                    {"path": "agent_view/model", "value": "haiku", "encrypted": 0}
                ]
            elif idx == 1:
                # workspace
                cur.fetchall.return_value = [
                    {"path": "agent_view/model", "value": "sonnet", "encrypted": 0}
                ]
            else:
                # agent_view
                cur.fetchall.return_value = [
                    {"path": "agent_view/model", "value": "opus", "encrypted": 0}
                ]
            return cur

        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(side_effect=make_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = build_scoped_overrides(conn, workspace_id=1, agent_view_id=10)
        assert result["agent_view/model"] == ("opus", False)

    def test_global_inherited_when_no_override(self):
        call_count = [0]

        def make_cursor():
            cur = MagicMock()
            idx = call_count[0]
            call_count[0] += 1
            if idx == 0:
                # global has both model and personality
                cur.fetchall.return_value = [
                    {"path": "agent_view/model", "value": "sonnet", "encrypted": 0},
                    {"path": "agent_view/claude/personality", "value": "be helpful", "encrypted": 0},
                ]
            elif idx == 1:
                # workspace overrides only model
                cur.fetchall.return_value = [
                    {"path": "agent_view/model", "value": "opus", "encrypted": 0},
                ]
            else:
                # agent_view has nothing
                cur.fetchall.return_value = []
            return cur

        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(side_effect=make_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = build_scoped_overrides(conn, workspace_id=1, agent_view_id=10)
        assert result["agent_view/model"] == ("opus", False)
        assert result["agent_view/claude/personality"] == ("be helpful", False)


class TestScopedConfigSet:
    def test_inserts_unencrypted(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        scoped_config_set(conn, "mymod/url", "https://test.com")
        cursor.execute.assert_called_once()
        args = cursor.execute.call_args[0]
        assert "INSERT INTO core_config_data" in args[0]
        assert args[1] == ("default", 0, "mymod/url", "https://test.com", 0)

    def test_inserts_with_scope(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        scoped_config_set(conn, "mymod/url", "https://ws.com", scope="workspace", scope_id=3)
        args = cursor.execute.call_args[0]
        assert args[1][0] == "workspace"
        assert args[1][1] == 3

    def test_inserts_encrypted(self, monkeypatch):
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "test-key")
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        scoped_config_set(conn, "mymod/token", "secret", encrypted=True)
        args = cursor.execute.call_args[0]
        stored_value = args[1][3]
        assert stored_value.startswith("aes256s:")
        assert args[1][4] == 1


class TestScope:
    def test_constants(self):
        assert Scope.DEFAULT == "default"
        assert Scope.WORKSPACE == "workspace"
        assert Scope.AGENT_VIEW == "agent_view"
