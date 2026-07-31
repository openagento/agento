"""Tests for jira_periodic_tasks onboarding flow."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from agento.modules.jira.src.toolbox_client import ToolboxAPIError
from agento.modules.jira_periodic_tasks.src.onboarding import (
    PeriodicTasksOnboarding,
    _resolve_admin_auth,
)


def _mock_conn(db_overrides=None):
    """Create a mock DB connection with configurable core_config_data rows.

    db_overrides: {path: value} dict — converted to (value, encrypted) tuples
    matching the real load_db_overrides return format.
    """
    conn = MagicMock()
    cursor = MagicMock()

    rows = []
    if db_overrides:
        for path, value in db_overrides.items():
            rows.append({"path": path, "value": value, "encrypted": 0})

    cursor.fetchall.return_value = rows
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


class TestIsComplete:
    def test_complete_when_both_configs_exist(self):
        conn = _mock_conn({
            "jira_periodic_tasks/jira_status": "Periodic",
            "jira_periodic_tasks/jira_frequency_field": "customfield_10100",
        })
        ob = PeriodicTasksOnboarding()
        assert ob.is_complete(conn) is True

    def test_incomplete_when_status_missing(self):
        conn = _mock_conn({
            "jira_periodic_tasks/jira_frequency_field": "customfield_10100",
        })
        ob = PeriodicTasksOnboarding()
        assert ob.is_complete(conn) is False

    def test_incomplete_when_field_missing(self):
        conn = _mock_conn({
            "jira_periodic_tasks/jira_status": "Periodic",
        })
        ob = PeriodicTasksOnboarding()
        assert ob.is_complete(conn) is False

    def test_incomplete_when_both_missing(self):
        conn = _mock_conn({})
        ob = PeriodicTasksOnboarding()
        assert ob.is_complete(conn) is False

    def test_incomplete_when_status_is_empty_string(self):
        conn = _mock_conn({
            "jira_periodic_tasks/jira_status": "",
            "jira_periodic_tasks/jira_frequency_field": "customfield_10100",
        })
        ob = PeriodicTasksOnboarding()
        assert ob.is_complete(conn) is False

    def test_incomplete_when_field_is_empty_string(self):
        conn = _mock_conn({
            "jira_periodic_tasks/jira_status": "Periodic",
            "jira_periodic_tasks/jira_frequency_field": "",
        })
        ob = PeriodicTasksOnboarding()
        assert ob.is_complete(conn) is False


class TestDescribe:
    def test_returns_description(self):
        ob = PeriodicTasksOnboarding()
        assert "Jira status" in ob.describe()
        assert "Frequency" in ob.describe()


class TestResolveAdminAuth:
    def test_pairs_admin_token_with_jira_admin_user_when_set(self):
        auth = _resolve_admin_auth({
            "jira/jira_admin_token": ("admin-secret", False),
            "jira/jira_user": ("bot@example.com", False),
            "jira/jira_admin_user": ("siteadmin@example.com", False),
        })
        assert auth == {"auth_user": "siteadmin@example.com", "auth_token": "admin-secret"}

    def test_falls_back_to_jira_user_when_admin_user_unset(self):
        auth = _resolve_admin_auth({
            "jira/jira_admin_token": ("admin-secret", False),
            "jira/jira_user": ("bot@example.com", False),
        })
        assert auth == {"auth_user": "bot@example.com", "auth_token": "admin-secret"}

    def test_none_when_no_admin_token(self):
        assert _resolve_admin_auth({"jira/jira_user": ("bot@example.com", False)}) is None

    def test_none_when_no_user_at_all(self):
        assert _resolve_admin_auth({"jira/jira_admin_token": ("admin-secret", False)}) is None


def _make_toolbox_mock(responses=None):
    """Create a ToolboxClient mock with configurable jira_request responses."""
    toolbox = MagicMock()
    default_responses = {
        ("GET", "/rest/api/3/myself"): {"accountId": "123"},
        ("GET", "/rest/api/3/project/AI"): {"id": "10001", "style": "classic"},
        ("GET", "/rest/api/3/project/AI/statuses"): [
            {"statuses": [{"name": "Periodic", "id": "100"}]}
        ],
        ("GET", "/rest/api/3/field"): [
            {"name": "Frequency", "id": "customfield_10100", "custom": True,
             "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"}}
        ],
        ("GET", "/rest/api/3/field/customfield_10100/context"): {"values": [{"id": "ctx1"}]},
        ("GET", "/rest/api/3/field/customfield_10100/context/ctx1/option"): {
            "values": [{"value": "Every 5min"}]
        },
        ("POST", "/rest/api/3/field/customfield_10100/context/ctx1/option"): {},
        ("GET", "/rest/api/3/screens"): {"values": [{"id": "1", "name": "Default"}]},
    }
    if responses:
        default_responses.update(responses)

    def jira_request_side_effect(method, path, body=None, **kwargs):
        key = (method, path)
        for k, v in default_responses.items():
            if k == key:
                if isinstance(v, Exception):
                    raise v
                return v
        # Tabs and fields for screen mapping
        if "/tabs" in path and "/fields" not in path:
            return [{"id": "tab1"}]
        return {}

    toolbox.jira_request.side_effect = jira_request_side_effect
    return toolbox


class TestRun:
    @patch("agento.modules.jira_periodic_tasks.src.onboarding.ToolboxClient")
    @patch("agento.modules.jira_periodic_tasks.src.onboarding.get_module_config")
    @patch("agento.modules.jira_periodic_tasks.src.onboarding.config_set")
    def test_happy_path(self, mock_config_set, mock_get_config, mock_toolbox_cls, monkeypatch):
        mock_get_config.return_value = {"toolbox_url": "http://toolbox:3001"}
        toolbox = _make_toolbox_mock()
        mock_toolbox_cls.return_value = toolbox

        inputs = iter(["", ""])  # status name, field name (project read from DB)
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        conn = _mock_conn({"jira/jira_projects": '["AI"]'})
        ob = PeriodicTasksOnboarding()
        config = {"frequency_map": {"Every 5min": "*/5 * * * *", "Daily at 8:00": "0 8 * * *"}}

        ob.run(conn, config, logging.getLogger("test"))

        assert mock_config_set.call_count == 2
        calls = {c.args[1]: c.args[2] for c in mock_config_set.call_args_list}
        assert calls["jira_periodic_tasks/jira_status"] == "Periodic"
        assert calls["jira_periodic_tasks/jira_frequency_field"] == "customfield_10100"
        conn.commit.assert_called_once()

    @patch("agento.modules.jira_periodic_tasks.src.onboarding.ToolboxClient")
    @patch("agento.modules.jira_periodic_tasks.src.onboarding.get_module_config")
    @patch("agento.modules.jira_periodic_tasks.src.onboarding.config_set")
    def test_uses_admin_auth_for_field_creation(self, mock_config_set, mock_get_config, mock_toolbox_cls, monkeypatch, capsys):
        mock_get_config.return_value = {"toolbox_url": "http://toolbox:3001"}
        toolbox = _make_toolbox_mock({
            ("GET", "/rest/api/3/field"): [],  # field not found, will create
            ("POST", "/rest/api/3/field"): {"id": "customfield_10200", "name": "Frequency"},
            ("GET", "/rest/api/3/field/customfield_10200/context"): {"values": [{"id": "ctx1"}]},
            ("GET", "/rest/api/3/field/customfield_10200/context/ctx1/option"): {"values": []},
            ("POST", "/rest/api/3/field/customfield_10200/context/ctx1/option"): {},
        })
        mock_toolbox_cls.return_value = toolbox

        inputs = iter(["", ""])  # status, field
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        conn = _mock_conn({
            "jira/jira_projects": '["AI"]',
            "jira/jira_user": "agent@example.com",
            "jira/jira_admin_token": "admin-secret",
        })
        ob = PeriodicTasksOnboarding()
        config = {"frequency_map": {"Every 5min": "*/5 * * * *"}}
        ob.run(conn, config, logging.getLogger("test"))

        output = capsys.readouterr().out
        assert "Using admin credentials" in output

        # Verify admin auth was passed to field creation call
        create_call = [
            c for c in toolbox.jira_request.call_args_list
            if c.args[0] == "POST" and c.args[1] == "/rest/api/3/field"
        ]
        assert len(create_call) == 1
        assert create_call[0].kwargs.get("auth_user") == "agent@example.com"
        assert create_call[0].kwargs.get("auth_token") == "admin-secret"

    @patch("agento.modules.jira_periodic_tasks.src.onboarding.ToolboxClient")
    @patch("agento.modules.jira_periodic_tasks.src.onboarding.get_module_config")
    def test_aborts_on_toolbox_unreachable(self, mock_get_config, mock_toolbox_cls, capsys):
        mock_get_config.return_value = {"toolbox_url": "http://toolbox:3001"}
        toolbox = MagicMock()
        mock_toolbox_cls.return_value = toolbox
        toolbox.jira_request.side_effect = Exception("Connection refused")

        conn = _mock_conn({})
        ob = PeriodicTasksOnboarding()
        ob.run(conn, {}, logging.getLogger("test"))

        output = capsys.readouterr().out
        assert "Toolbox not reachable" in output

    @patch("agento.modules.jira_periodic_tasks.src.onboarding.get_module_config")
    def test_aborts_when_toolbox_url_missing(self, mock_get_config, capsys):
        mock_get_config.return_value = {}

        conn = _mock_conn({})
        ob = PeriodicTasksOnboarding()
        ob.run(conn, {}, logging.getLogger("test"))

        output = capsys.readouterr().out
        assert "core/toolbox/url not configured" in output

    @patch("agento.modules.jira_periodic_tasks.src.onboarding.ToolboxClient")
    @patch("agento.modules.jira_periodic_tasks.src.onboarding.get_module_config")
    def test_aborts_on_status_creation_failure(self, mock_get_config, mock_toolbox_cls, monkeypatch, capsys):
        mock_get_config.return_value = {"toolbox_url": "http://toolbox:3001"}
        toolbox = _make_toolbox_mock({
            ("GET", "/rest/api/3/project/AI/statuses"): [{"statuses": []}],
            ("POST", "/rest/api/3/statuses"): ToolboxAPIError(403, "Forbidden"),
        })
        mock_toolbox_cls.return_value = toolbox

        inputs = iter([""])  # status name (project read from DB)
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        conn = _mock_conn({"jira/jira_projects": '["AI"]'})
        ob = PeriodicTasksOnboarding()
        ob.run(conn, {}, logging.getLogger("test"))

        output = capsys.readouterr().out
        assert "Failed to create status" in output

    @patch("agento.modules.jira_periodic_tasks.src.onboarding.ToolboxClient")
    @patch("agento.modules.jira_periodic_tasks.src.onboarding.get_module_config")
    def test_aborts_on_field_creation_failure(self, mock_get_config, mock_toolbox_cls, monkeypatch, capsys):
        mock_get_config.return_value = {"toolbox_url": "http://toolbox:3001"}
        toolbox = _make_toolbox_mock({
            ("GET", "/rest/api/3/field"): [],  # field not found
            ("POST", "/rest/api/3/field"): ToolboxAPIError(403, "Admin required"),
        })
        mock_toolbox_cls.return_value = toolbox

        inputs = iter(["", ""])  # status, field (project read from DB)
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        conn = _mock_conn({"jira/jira_projects": '["AI"]'})
        ob = PeriodicTasksOnboarding()
        ob.run(conn, {}, logging.getLogger("test"))

        output = capsys.readouterr().out
        assert "Failed to create field" in output

    @patch("agento.modules.jira_periodic_tasks.src.onboarding.ToolboxClient")
    @patch("agento.modules.jira_periodic_tasks.src.onboarding.get_module_config")
    @patch("agento.modules.jira_periodic_tasks.src.onboarding.config_set")
    def test_aborts_on_option_sync_failure(self, mock_config_set, mock_get_config, mock_toolbox_cls, monkeypatch, capsys):
        mock_get_config.return_value = {"toolbox_url": "http://toolbox:3001"}
        toolbox = _make_toolbox_mock({
            ("GET", "/rest/api/3/field/customfield_10100/context"): ToolboxAPIError(500, "Server error"),
        })
        mock_toolbox_cls.return_value = toolbox

        inputs = iter(["", ""])  # status, field (project read from DB)
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        conn = _mock_conn({"jira/jira_projects": '["AI"]'})
        ob = PeriodicTasksOnboarding()
        config = {"frequency_map": {"Every 5min": "*/5 * * * *"}}
        ob.run(conn, config, logging.getLogger("test"))

        output = capsys.readouterr().out
        assert "Could not get field contexts" in output
        # Config should NOT be saved on option sync failure
        mock_config_set.assert_not_called()

    @patch("agento.modules.jira_periodic_tasks.src.onboarding.ToolboxClient")
    @patch("agento.modules.jira_periodic_tasks.src.onboarding.get_module_config")
    def test_does_not_match_multiselect_field(self, mock_get_config, mock_toolbox_cls, monkeypatch, capsys):
        """_find_field should not match multiselect or cascading select fields."""
        mock_get_config.return_value = {"toolbox_url": "http://toolbox:3001"}
        toolbox = _make_toolbox_mock({
            ("GET", "/rest/api/3/field"): [
                {"name": "Frequency", "id": "customfield_99", "custom": True,
                 "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:multiselect"}},
            ],
            ("POST", "/rest/api/3/field"): {"id": "customfield_10200", "name": "Frequency"},
            ("GET", "/rest/api/3/field/customfield_10200/context"): {"values": [{"id": "ctx1"}]},
            ("GET", "/rest/api/3/field/customfield_10200/context/ctx1/option"): {"values": []},
            ("POST", "/rest/api/3/field/customfield_10200/context/ctx1/option"): {},
        })
        mock_toolbox_cls.return_value = toolbox

        inputs = iter(["", ""])  # status, field (project read from DB)
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        conn = _mock_conn({"jira/jira_projects": '["AI"]'})
        ob = PeriodicTasksOnboarding()
        config = {"frequency_map": {"Every 5min": "*/5 * * * *"}}
        ob.run(conn, config, logging.getLogger("test"))

        output = capsys.readouterr().out
        # Should NOT find the multiselect field, should create a new one
        assert "not found. Creating" in output
