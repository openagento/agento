"""Tests for config:set / config:remove CLI commands + scope restrictions."""
from __future__ import annotations

import argparse
import io
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.cli.config import (
    ConfigRemoveCommand,
    ConfigSchemaCommand,
    ConfigSetCommand,
    _validate_config_path,
)
from agento.framework.workspace import AgentView


def _mock_conn():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


def _make_agent_view(id=42, code="dev_01"):
    now = datetime(2026, 1, 1)
    return AgentView(
        id=id, workspace_id=10, code=code, label="Dev",
        is_active=True, created_at=now, updated_at=now,
    )


@pytest.fixture
def fake_module(tmp_path: Path):
    module_dir = tmp_path / "testmod"
    module_dir.mkdir()
    (module_dir / "module.json").write_text(json.dumps({
        "name": "testmod",
        "tools": [
            {
                "name": "mytool",
                "fields": {
                    "api_key": {
                        "type": "string",
                        "label": "API Key",
                        "showInDefault": True,
                        "showInWorkspace": False,
                        "showInAgentView": False,
                    },
                    "region": {"type": "string", "label": "Region"},
                },
            }
        ],
    }))
    system = {
        "timezone": {
            "type": "string",
            "label": "IANA timezone",
            "showInDefault": True,
            "showInWorkspace": False,
            "showInAgentView": False,
        },
        "model": {"type": "string", "label": "Model"},
    }
    (module_dir / "system.json").write_text(json.dumps(system))
    return module_dir


class TestValidateConfigPathScope:
    def test_global_only_field_rejected_at_agent_view(self, fake_module, capsys):
        with patch("agento.framework.core_config._find_module_dir", return_value=fake_module):
            ok = _validate_config_path("testmod/timezone", scope="agent_view")
        assert ok is False
        out = capsys.readouterr().out
        assert "cannot be set at scope 'agent_view'" in out
        assert "default" in out

    def test_global_only_field_allowed_at_default(self, fake_module):
        with patch("agento.framework.core_config._find_module_dir", return_value=fake_module):
            assert _validate_config_path("testmod/timezone", scope="default") is True

    def test_field_without_flags_allowed_at_any_scope(self, fake_module):
        with patch("agento.framework.core_config._find_module_dir", return_value=fake_module):
            assert _validate_config_path("testmod/model", scope="default") is True
            assert _validate_config_path("testmod/model", scope="workspace") is True
            assert _validate_config_path("testmod/model", scope="agent_view") is True

    def test_default_scope_parameter_is_backward_compatible(self, fake_module):
        with patch("agento.framework.core_config._find_module_dir", return_value=fake_module):
            assert _validate_config_path("testmod/model") is True

    def test_unknown_field_still_rejected(self, fake_module, capsys):
        with patch("agento.framework.core_config._find_module_dir", return_value=fake_module):
            ok = _validate_config_path("testmod/nonexistent", scope="default")
        assert ok is False
        out = capsys.readouterr().out
        assert "not found" in out

    def test_unknown_module_rejected(self):
        with patch("agento.framework.core_config._find_module_dir", return_value=None):
            assert _validate_config_path("mystery/foo", scope="default") is False

    def test_tool_field_scope_restriction_enforced(self, fake_module, capsys):
        with patch("agento.framework.core_config._find_module_dir", return_value=fake_module):
            ok = _validate_config_path(
                "testmod/tools/mytool/api_key", scope="agent_view"
            )
        assert ok is False
        out = capsys.readouterr().out
        assert "cannot be set at scope 'agent_view'" in out

    def test_tool_field_without_flags_allowed_at_any_scope(self, fake_module):
        with patch("agento.framework.core_config._find_module_dir", return_value=fake_module):
            assert _validate_config_path(
                "testmod/tools/mytool/region", scope="agent_view"
            ) is True

    def test_tool_field_global_only_allowed_at_default(self, fake_module):
        with patch("agento.framework.core_config._find_module_dir", return_value=fake_module):
            assert _validate_config_path(
                "testmod/tools/mytool/api_key", scope="default"
            ) is True

    def test_unknown_tool_does_not_crash(self, fake_module):
        with patch("agento.framework.core_config._find_module_dir", return_value=fake_module):
            assert _validate_config_path(
                "testmod/tools/unknown_tool/field", scope="default"
            ) is True


class TestConfigSchemaUnreachableWarning:
    def test_warns_when_all_show_in_flags_are_false(self, tmp_path: Path, capsys):
        module_dir = tmp_path / "stale_mod"
        module_dir.mkdir()
        (module_dir / "module.json").write_text(json.dumps({
            "name": "stale_mod",
            "version": "1.0.0",
            "description": "",
        }))
        (module_dir / "system.json").write_text(json.dumps({
            "unreachable_field": {
                "type": "string",
                "label": "Nobody can set me",
                "showInDefault": False,
                "showInWorkspace": False,
                "showInAgentView": False,
            },
            "ok_field": {"type": "string", "label": "Fine"},
        }))

        with patch(
            "agento.framework.bootstrap.CORE_MODULES_DIR", str(tmp_path)
        ), patch(
            "agento.framework.bootstrap.USER_MODULES_DIR", "/does/not/exist"
        ):
            cmd = ConfigSchemaCommand()
            args = argparse.Namespace(module="stale_mod", as_json=False)
            cmd.execute(args)

        out = capsys.readouterr().out
        assert "unreachable" in out.lower()
        assert "stale_mod/unreachable_field" in out
        assert "stale_mod/ok_field" not in out.split("unreachable")[1]


def _set_args(path="jira/jira_token", value=None, scope="default", scope_id=0, agent_view=None):
    return argparse.Namespace(
        path=path, value=value, scope=scope, scope_id=scope_id, agent_view=agent_view,
    )


def _remove_args(path="jira/jira_token", scope="default", scope_id=0, agent_view=None):
    return argparse.Namespace(
        path=path, scope=scope, scope_id=scope_id, agent_view=agent_view,
    )


class TestConfigSetCommand:
    @patch("agento.framework.cli.config.get_connection_or_exit")
    @patch("agento.framework.cli.config._load_framework_config")
    @patch("agento.framework.cli.config._validate_config_path", return_value=True)
    @patch("agento.framework.cli.config._validate_config_value", return_value=True)
    @patch("agento.framework.event_manager.get_event_manager")
    @patch("agento.framework.core_config.config_set_auto_encrypt")
    def test_reads_value_from_stdin_pipe(
        self, mock_write, mock_events, _vv, _vp, mock_config, mock_conn_fn,
        monkeypatch,
    ):
        mock_config.return_value = ({}, None, None)
        mock_conn_fn.return_value = _mock_conn()[0]
        mock_write.return_value = False
        mock_events.return_value = MagicMock()

        piped = io.StringIO("the-value")
        monkeypatch.setattr(piped, "isatty", lambda: False)
        monkeypatch.setattr("sys.stdin", piped)

        ConfigSetCommand().execute(_set_args(value=None))

        mock_write.assert_called_once()
        assert mock_write.call_args.args[2] == "the-value"

    @patch("agento.framework.cli.config.get_connection_or_exit")
    @patch("agento.framework.cli.config._load_framework_config")
    @patch("agento.framework.cli.config._validate_config_path", return_value=True)
    @patch("agento.framework.cli.config._validate_config_value", return_value=True)
    @patch("agento.framework.event_manager.get_event_manager")
    @patch("agento.framework.core_config.config_set_auto_encrypt")
    def test_reads_value_from_stdin_tty_strips_trailing_newline(
        self, mock_write, mock_events, _vv, _vp, mock_config, mock_conn_fn,
        monkeypatch, capsys,
    ):
        mock_config.return_value = ({}, None, None)
        mock_conn_fn.return_value = _mock_conn()[0]
        mock_write.return_value = False
        mock_events.return_value = MagicMock()

        tty_stream = io.StringIO("the-value\n")
        monkeypatch.setattr(tty_stream, "isatty", lambda: True)
        monkeypatch.setattr("sys.stdin", tty_stream)

        ConfigSetCommand().execute(_set_args(value=None))

        assert mock_write.call_args.args[2] == "the-value"
        # Prompt goes to stderr
        assert "Paste value" in capsys.readouterr().err

    @patch("agento.framework.cli.config.get_connection_or_exit")
    @patch("agento.framework.cli.config._load_framework_config")
    @patch("agento.framework.cli.config._validate_config_path", return_value=True)
    @patch("agento.framework.cli.config._validate_config_value", return_value=True)
    @patch("agento.framework.event_manager.get_event_manager")
    @patch("agento.framework.core_config.config_set_auto_encrypt")
    @patch("agento.framework.workspace.get_agent_view_by_code")
    def test_agent_view_flag_resolves_scope(
        self, mock_av, mock_write, mock_events, _vv, _vp, mock_config, mock_conn_fn,
    ):
        mock_config.return_value = ({}, None, None)
        mock_conn_fn.return_value = _mock_conn()[0]
        mock_write.return_value = True
        mock_events.return_value = MagicMock()
        mock_av.return_value = _make_agent_view(id=42, code="dev_01")

        args = _set_args(value="v", agent_view="dev_01")
        ConfigSetCommand().execute(args)

        call = mock_write.call_args
        assert call.kwargs.get("scope") == "agent_view"
        assert call.kwargs.get("scope_id") == 42

    @patch("agento.framework.cli.config.get_connection_or_exit")
    @patch("agento.framework.cli.config._load_framework_config")
    def test_agent_view_flag_conflicts_with_scope_id(
        self, mock_config, mock_conn_fn,
    ):
        mock_config.return_value = ({}, None, None)
        mock_conn_fn.return_value = _mock_conn()[0]

        args = _set_args(value="v", agent_view="dev_01", scope_id=99)
        with pytest.raises(SystemExit):
            ConfigSetCommand().execute(args)

    @patch("agento.framework.cli.config.get_connection_or_exit")
    @patch("agento.framework.cli.config._load_framework_config")
    def test_agent_view_flag_conflicts_with_non_agent_view_scope(
        self, mock_config, mock_conn_fn,
    ):
        mock_config.return_value = ({}, None, None)
        mock_conn_fn.return_value = _mock_conn()[0]

        args = _set_args(value="v", agent_view="dev_01", scope="workspace")
        with pytest.raises(SystemExit):
            ConfigSetCommand().execute(args)

    @patch("agento.framework.cli.config.get_connection_or_exit")
    @patch("agento.framework.cli.config._load_framework_config")
    @patch("agento.framework.cli.config._validate_config_path", return_value=True)
    @patch("agento.framework.cli.config._validate_config_value", return_value=False)
    @patch("agento.framework.core_config.config_set_auto_encrypt")
    def test_invalid_value_exits_nonzero(
        self, mock_write, _vv, _vp, mock_config, mock_conn_fn,
    ):
        """A rejected select value must fail the process, not print and exit 0 —
        scripts (and the e2e suite) treat rc=0 as "the value was set"."""
        mock_config.return_value = ({}, None, None)
        mock_conn_fn.return_value = _mock_conn()[0]

        with pytest.raises(SystemExit) as exc:
            ConfigSetCommand().execute(_set_args(value="codex"))

        assert exc.value.code == 1
        mock_write.assert_not_called()

    @patch("agento.framework.cli.config.get_connection_or_exit")
    @patch("agento.framework.cli.config._load_framework_config")
    @patch("agento.framework.cli.config._validate_config_path", return_value=False)
    @patch("agento.framework.core_config.config_set_auto_encrypt")
    def test_invalid_path_exits_nonzero(
        self, mock_write, _vp, mock_config, mock_conn_fn,
    ):
        mock_config.return_value = ({}, None, None)
        mock_conn_fn.return_value = _mock_conn()[0]

        with pytest.raises(SystemExit) as exc:
            ConfigSetCommand().execute(_set_args(value="v"))

        assert exc.value.code == 1
        mock_write.assert_not_called()

    def test_path_without_slash_exits_nonzero(self):
        with pytest.raises(SystemExit) as exc:
            ConfigSetCommand().execute(_set_args(path="jira", value="v"))

        assert exc.value.code == 1

    @patch("agento.framework.cli.config.get_connection_or_exit")
    @patch("agento.framework.cli.config._load_framework_config")
    @patch("agento.framework.workspace.get_agent_view_by_code")
    def test_agent_view_flag_unknown_code_exits(
        self, mock_av, mock_config, mock_conn_fn,
    ):
        mock_config.return_value = ({}, None, None)
        mock_conn_fn.return_value = _mock_conn()[0]
        mock_av.return_value = None

        args = _set_args(value="v", agent_view="nope")
        with pytest.raises(SystemExit):
            ConfigSetCommand().execute(args)


class TestConfigRemoveCommand:
    @patch("agento.framework.cli.config.get_connection_or_exit")
    @patch("agento.framework.cli.config._load_framework_config")
    @patch("agento.framework.core_config.config_delete")
    @patch("agento.framework.workspace.get_agent_view_by_code")
    def test_agent_view_flag_resolves_scope(
        self, mock_av, mock_delete, mock_config, mock_conn_fn,
    ):
        mock_config.return_value = ({}, None, None)
        mock_conn_fn.return_value = _mock_conn()[0]
        mock_delete.return_value = True
        mock_av.return_value = _make_agent_view(id=42, code="dev_01")

        args = _remove_args(agent_view="dev_01")
        ConfigRemoveCommand().execute(args)

        call = mock_delete.call_args
        assert call.kwargs.get("scope") == "agent_view"
        assert call.kwargs.get("scope_id") == 42

    @patch("agento.framework.cli.config.get_connection_or_exit")
    @patch("agento.framework.cli.config._load_framework_config")
    @patch("agento.framework.core_config.config_delete")
    def test_default_scope_passthrough(
        self, mock_delete, mock_config, mock_conn_fn,
    ):
        mock_config.return_value = ({}, None, None)
        mock_conn_fn.return_value = _mock_conn()[0]
        mock_delete.return_value = True

        ConfigRemoveCommand().execute(_remove_args())

        call = mock_delete.call_args
        assert call.kwargs.get("scope") == "default"
        assert call.kwargs.get("scope_id") == 0
