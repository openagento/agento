"""Tests for tool:list, tool:enable, tool:disable CLI commands."""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

from agento.modules.agent_view.src.commands.tool_disable import ToolDisableCommand
from agento.modules.agent_view.src.commands.tool_enable import ToolEnableCommand
from agento.modules.agent_view.src.commands.tool_list import ToolListCommand


def _mock_conn():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


def _make_manifest(name, tools):
    m = MagicMock()
    m.name = name
    m.tools = tools
    # A real ModuleManifest always carries a Path; ScopedConfigService reads each
    # manifest's config.json when resolving a module-agnostic tools/<name>/is_enabled
    # key. Point it somewhere absent so the defaults read yields {}.
    m.path = Path("/nonexistent/module")
    return m


class TestToolListCommand:
    def test_properties(self):
        cmd = ToolListCommand()
        assert cmd.name == "tool:list"
        assert cmd.shortcut == "tl:li"
        assert "tool" in cmd.help.lower()

    @patch("agento.framework.workspace.get_agent_view_by_code")
    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.framework.bootstrap.get_manifests")
    @patch("agento.framework.db.get_connection")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_list_all_disabled_by_default(self, mock_config, mock_conn_fn, mock_manifests, mock_overrides, mock_av, capsys):
        # Opt-in: with no override rows, every tool lists as disabled.
        mock_config.return_value = ({}, None, None)
        conn, _ = _mock_conn()
        mock_conn_fn.return_value = conn
        mock_manifests.return_value = [
            _make_manifest("jira", [{"name": "jira_search", "type": "mysql"}]),
            _make_manifest("slack", [{"name": "slack_post", "type": "mysql"}]),
        ]
        mock_overrides.return_value = {}

        args = argparse.Namespace(agent_view_code=None)
        ToolListCommand().execute(args)

        output = capsys.readouterr().out
        assert "jira_search" in output
        assert "slack_post" in output
        assert "disabled" in output
        assert "enabled" not in output

    @patch("agento.framework.workspace.get_agent_view_by_code")
    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.framework.bootstrap.get_manifests")
    @patch("agento.framework.db.get_connection")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_list_with_mixed_status(self, mock_config, mock_conn_fn, mock_manifests, mock_overrides, mock_av, capsys):
        mock_config.return_value = ({}, None, None)
        conn, _ = _mock_conn()
        mock_conn_fn.return_value = conn
        mock_manifests.return_value = [
            _make_manifest("jira", [
                {"name": "jira_search", "type": "mysql"},
                {"name": "jira_create", "type": "mysql"},
            ]),
        ]
        # Opt-in: jira_create explicitly enabled; jira_search explicitly disabled.
        mock_overrides.return_value = {
            "tools/jira_create/is_enabled": ("1", False),
            "tools/jira_search/is_enabled": ("0", False),
        }

        args = argparse.Namespace(agent_view_code=None)
        ToolListCommand().execute(args)

        output = capsys.readouterr().out
        assert "disabled" in output
        assert "enabled" in output

    @patch("agento.framework.workspace.get_agent_view_by_code")
    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.framework.bootstrap.get_manifests")
    @patch("agento.framework.db.get_connection")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_list_with_agent_view(self, mock_config, mock_conn_fn, mock_manifests, mock_overrides, mock_av, capsys):
        mock_config.return_value = ({}, None, None)
        conn, _ = _mock_conn()
        mock_conn_fn.return_value = conn

        av = MagicMock()
        av.id = 5
        av.workspace_id = 2
        mock_av.return_value = av

        mock_manifests.return_value = [
            _make_manifest("jira", [{"name": "jira_search", "type": "mysql"}]),
        ]
        mock_overrides.return_value = {}

        args = argparse.Namespace(agent_view_code="developer")
        ToolListCommand().execute(args)

        mock_av.assert_called_once_with(conn, "developer")
        mock_overrides.assert_called_once_with(conn, agent_view_id=5, workspace_id=2)

    @patch("agento.framework.workspace.get_agent_view_by_code")
    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.framework.bootstrap.get_manifests")
    @patch("agento.framework.db.get_connection")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_list_agent_view_not_found(self, mock_config, mock_conn_fn, mock_manifests, mock_overrides, mock_av, capsys):
        mock_config.return_value = ({}, None, None)
        conn, _ = _mock_conn()
        mock_conn_fn.return_value = conn
        mock_av.return_value = None

        args = argparse.Namespace(agent_view_code="nonexistent")
        ToolListCommand().execute(args)

        output = capsys.readouterr().out
        assert "Error" in output
        assert "nonexistent" in output

    @patch("agento.framework.workspace.get_agent_view_by_code")
    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.framework.bootstrap.get_manifests")
    @patch("agento.framework.db.get_connection")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_list_no_tools(self, mock_config, mock_conn_fn, mock_manifests, mock_overrides, mock_av, capsys):
        mock_config.return_value = ({}, None, None)
        conn, _ = _mock_conn()
        mock_conn_fn.return_value = conn
        mock_manifests.return_value = []
        mock_overrides.return_value = {}

        args = argparse.Namespace(agent_view_code=None)
        ToolListCommand().execute(args)

        output = capsys.readouterr().out
        assert "No tools registered" in output


class TestToolEnableCommand:
    def test_properties(self):
        cmd = ToolEnableCommand()
        assert cmd.name == "tool:enable"
        assert cmd.shortcut == "tl:en"

    def test_rejects_name_with_spaces(self):
        import pytest
        args = argparse.Namespace(tool_name="jira search", scope="default", scope_id=0, agent_view_code=None)
        with pytest.raises(SystemExit, match="Invalid tool name"):
            ToolEnableCommand().execute(args)

    def test_rejects_name_with_special_chars(self):
        import pytest
        args = argparse.Namespace(tool_name="jira@search!", scope="default", scope_id=0, agent_view_code=None)
        with pytest.raises(SystemExit, match="Invalid tool name"):
            ToolEnableCommand().execute(args)

    def test_rejects_name_with_uppercase(self):
        import pytest
        args = argparse.Namespace(tool_name="JiraSearch", scope="default", scope_id=0, agent_view_code=None)
        with pytest.raises(SystemExit, match="Invalid tool name"):
            ToolEnableCommand().execute(args)

    def test_rejects_empty_name(self):
        import pytest
        args = argparse.Namespace(tool_name="", scope="default", scope_id=0, agent_view_code=None)
        with pytest.raises(SystemExit, match="Invalid tool name"):
            ToolEnableCommand().execute(args)

    @patch("agento.framework.scoped_config.scoped_config_set")
    @patch("agento.framework.db.get_connection")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_enable_default_scope(self, mock_config, mock_conn_fn, mock_set, capsys):
        mock_config.return_value = ({}, None, None)
        conn, _ = _mock_conn()
        mock_conn_fn.return_value = conn

        args = argparse.Namespace(tool_name="jira_search", scope="default", scope_id=0, agent_view_code=None)
        ToolEnableCommand().execute(args)

        mock_set.assert_called_once_with(
            conn,
            "tools/jira_search/is_enabled",
            "1",
            scope="default",
            scope_id=0,
        )
        conn.commit.assert_called_once()
        output = capsys.readouterr().out
        assert "Enabled" in output
        assert "jira_search" in output

    @patch("agento.framework.scoped_config.scoped_config_set")
    @patch("agento.framework.db.get_connection")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_enable_agent_view_scope(self, mock_config, mock_conn_fn, mock_set, capsys):
        mock_config.return_value = ({}, None, None)
        conn, _ = _mock_conn()
        mock_conn_fn.return_value = conn

        args = argparse.Namespace(tool_name="slack_post", scope="agent_view", scope_id=5, agent_view_code=None)
        ToolEnableCommand().execute(args)

        mock_set.assert_called_once_with(
            conn,
            "tools/slack_post/is_enabled",
            "1",
            scope="agent_view",
            scope_id=5,
        )


class TestToolDisableCommand:
    def test_properties(self):
        cmd = ToolDisableCommand()
        assert cmd.name == "tool:disable"
        assert cmd.shortcut == "tl:di"

    def test_rejects_name_with_spaces(self):
        import pytest
        args = argparse.Namespace(tool_name="jira search", scope="default", scope_id=0, agent_view_code=None)
        with pytest.raises(SystemExit, match="Invalid tool name"):
            ToolDisableCommand().execute(args)

    def test_rejects_name_with_special_chars(self):
        import pytest
        args = argparse.Namespace(tool_name="jira@search!", scope="default", scope_id=0, agent_view_code=None)
        with pytest.raises(SystemExit, match="Invalid tool name"):
            ToolDisableCommand().execute(args)

    def test_rejects_name_with_uppercase(self):
        import pytest
        args = argparse.Namespace(tool_name="JiraSearch", scope="default", scope_id=0, agent_view_code=None)
        with pytest.raises(SystemExit, match="Invalid tool name"):
            ToolDisableCommand().execute(args)

    def test_rejects_empty_name(self):
        import pytest
        args = argparse.Namespace(tool_name="", scope="default", scope_id=0, agent_view_code=None)
        with pytest.raises(SystemExit, match="Invalid tool name"):
            ToolDisableCommand().execute(args)

    @patch("agento.framework.scoped_config.scoped_config_set")
    @patch("agento.framework.db.get_connection")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_disable_default_scope(self, mock_config, mock_conn_fn, mock_set, capsys):
        mock_config.return_value = ({}, None, None)
        conn, _ = _mock_conn()
        mock_conn_fn.return_value = conn

        args = argparse.Namespace(tool_name="jira_search", scope="default", scope_id=0, agent_view_code=None)
        ToolDisableCommand().execute(args)

        mock_set.assert_called_once_with(
            conn,
            "tools/jira_search/is_enabled",
            "0",
            scope="default",
            scope_id=0,
        )
        conn.commit.assert_called_once()
        output = capsys.readouterr().out
        assert "Disabled" in output
        assert "jira_search" in output

    @patch("agento.framework.scoped_config.scoped_config_set")
    @patch("agento.framework.db.get_connection")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_disable_workspace_scope(self, mock_config, mock_conn_fn, mock_set, capsys):
        mock_config.return_value = ({}, None, None)
        conn, _ = _mock_conn()
        mock_conn_fn.return_value = conn

        args = argparse.Namespace(tool_name="browser", scope="workspace", scope_id=3, agent_view_code=None)
        ToolDisableCommand().execute(args)

        mock_set.assert_called_once_with(
            conn,
            "tools/browser/is_enabled",
            "0",
            scope="workspace",
            scope_id=3,
        )
