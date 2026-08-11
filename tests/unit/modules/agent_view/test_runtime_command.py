"""Tests for agent_view:runtime CLI command."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.agent_view_runtime import AgentViewRuntime
from agento.framework.workspace import AgentView, Workspace
from agento.modules.agent_view.src.commands.runtime_show import AgentViewRuntimeCommand

pytestmark = pytest.mark.usefixtures("builtin_harnesses")


def _make_agent_view(id=2, code="dev_01", workspace_id=1):
    now = datetime(2026, 1, 1)
    return AgentView(
        id=id, workspace_id=workspace_id, code=code, label="Dev",
        is_active=True, created_at=now, updated_at=now,
    )


def _make_workspace(id=1, code="it"):
    now = datetime(2026, 1, 1)
    return Workspace(
        id=id, code=code, label="IT",
        is_active=True, created_at=now, updated_at=now,
    )


def _mock_conn():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


def _args(code="dev_01", prompt=None, model=None, yolo=False):
    return argparse.Namespace(
        agent_view_code=code, prompt=prompt, model=model, yolo=yolo,
    )


# Asserted against the REAL ClaudeCommandBuilder rather than a stub, so a flag drifting
# between the interactive and headless forms fails here (the exact bug the old split
# CliInvoker/runner had: interactive silently lost the per-job --mcp-config).
_MCP_FLAGS = ["--mcp-config", ".mcp.json", "--strict-mcp-config"]
_INTERACTIVE = ["claude", *_MCP_FLAGS]


class TestAgentViewRuntimeCommand:
    def test_properties(self):
        cmd = AgentViewRuntimeCommand()
        assert cmd.name == "agent_view:runtime"
        assert cmd.shortcut == "av:ru"

    @patch("agento.framework.db.get_connection_or_exit")
    @patch("agento.framework.cli.runtime._load_framework_config")
    @patch("agento.framework.workspace.get_agent_view_by_code")
    def test_unknown_code_exits_with_error(
        self, mock_av_lookup, mock_config, mock_get_conn, capsys,
    ):
        mock_config.return_value = ({}, None, None)
        mock_get_conn.return_value = _mock_conn()
        mock_av_lookup.return_value = None

        with pytest.raises(SystemExit) as exc:
            AgentViewRuntimeCommand().execute(_args("unknown"))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "agent_view 'unknown' not found" in err

    @patch("agento.framework.agent_view_runtime.resolve_agent_view_runtime")
    @patch("agento.framework.db.get_connection_or_exit")
    @patch("agento.framework.cli.runtime._load_framework_config")
    @patch("agento.framework.workspace.get_agent_view_by_code")
    def test_interactive_includes_command(
        self, mock_av_lookup, mock_config, mock_get_conn, mock_resolve, capsys,
    ):
        mock_config.return_value = ({}, None, None)
        mock_get_conn.return_value = _mock_conn()
        av = _make_agent_view()
        mock_av_lookup.return_value = av
        mock_resolve.return_value = AgentViewRuntime(
            agent_view=av,
            workspace=_make_workspace(),
            harness="claude",
            provider="anthropic",
            model="claude-opus-4-6",
        )

        AgentViewRuntimeCommand().execute(_args())

        payload = json.loads(capsys.readouterr().out)
        assert payload["interactive_command"] == [*_INTERACTIVE, "--model", "claude-opus-4-6"]
        assert payload["headless_command"] is None
        assert payload["home"] == "/workspace/build/it/dev_01/current"
        assert payload["harness"] == "claude"

    @patch("agento.framework.agent_view_runtime.resolve_agent_view_runtime")
    @patch("agento.framework.db.get_connection_or_exit")
    @patch("agento.framework.cli.runtime._load_framework_config")
    @patch("agento.framework.workspace.get_agent_view_by_code")
    def test_yolo_adds_bypass_flag_to_interactive_command(
        self, mock_av_lookup, mock_config, mock_get_conn, mock_resolve, capsys,
    ):
        mock_config.return_value = ({}, None, None)
        mock_get_conn.return_value = _mock_conn()
        av = _make_agent_view()
        mock_av_lookup.return_value = av
        mock_resolve.return_value = AgentViewRuntime(
            agent_view=av,
            workspace=_make_workspace(),
            harness="claude",
            provider="anthropic",
            model="claude-opus-4-6",
        )

        AgentViewRuntimeCommand().execute(_args(yolo=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload["interactive_command"] == [
            *_INTERACTIVE, "--dangerously-skip-permissions",
            "--model", "claude-opus-4-6",
        ]

    @patch("agento.framework.agent_view_runtime.resolve_agent_view_runtime")
    @patch("agento.framework.db.get_connection_or_exit")
    @patch("agento.framework.cli.runtime._load_framework_config")
    @patch("agento.framework.workspace.get_agent_view_by_code")
    def test_prompt_includes_headless_command(
        self, mock_av_lookup, mock_config, mock_get_conn, mock_resolve, capsys,
    ):
        mock_config.return_value = ({}, None, None)
        mock_get_conn.return_value = _mock_conn()
        av = _make_agent_view()
        mock_av_lookup.return_value = av
        mock_resolve.return_value = AgentViewRuntime(
            agent_view=av,
            workspace=_make_workspace(),
            harness="claude",
            provider="anthropic",
            model="claude-opus-4-6",
        )

        AgentViewRuntimeCommand().execute(_args(prompt="hi there"))

        payload = json.loads(capsys.readouterr().out)
        assert payload["interactive_command"] == [*_INTERACTIVE, "--model", "claude-opus-4-6"]
        assert payload["headless_command"] == [
            "claude", "-p", "hi there", "--dangerously-skip-permissions",
            *_MCP_FLAGS, "--output-format", "stream-json", "--verbose",
            "--model", "claude-opus-4-6",
        ]

    @patch("agento.framework.agent_view_runtime.resolve_agent_view_runtime")
    @patch("agento.framework.db.get_connection_or_exit")
    @patch("agento.framework.cli.runtime._load_framework_config")
    @patch("agento.framework.workspace.get_agent_view_by_code")
    def test_model_override_used_in_headless_command(
        self, mock_av_lookup, mock_config, mock_get_conn, mock_resolve, capsys,
    ):
        mock_config.return_value = ({}, None, None)
        mock_get_conn.return_value = _mock_conn()
        av = _make_agent_view()
        mock_av_lookup.return_value = av
        mock_resolve.return_value = AgentViewRuntime(
            agent_view=av,
            workspace=_make_workspace(),
            harness="claude",
            provider="anthropic",
            model="default-model",
        )

        AgentViewRuntimeCommand().execute(
            _args(prompt="hi", model="override-model"),
        )

        payload = json.loads(capsys.readouterr().out)
        assert payload["headless_command"][-2:] == ["--model", "override-model"]
        assert payload["model"] == "default-model"  # unchanged; override is ad-hoc

    @patch("agento.framework.agent_view_runtime.resolve_agent_view_runtime")
    @patch("agento.framework.db.get_connection_or_exit")
    @patch("agento.framework.cli.runtime._load_framework_config")
    @patch("agento.framework.workspace.get_agent_view_by_code")
    def test_unregistered_harness_returns_null_commands(
        self, mock_av_lookup, mock_config, mock_get_conn, mock_resolve, capsys,
    ):
        mock_config.return_value = ({}, None, None)
        mock_get_conn.return_value = _mock_conn()
        av = _make_agent_view()
        mock_av_lookup.return_value = av
        mock_resolve.return_value = AgentViewRuntime(
            agent_view=av,
            workspace=_make_workspace(),
            harness="exotic",
            provider="whatever",
            model=None,
        )

        AgentViewRuntimeCommand().execute(_args(prompt="hi"))

        payload = json.loads(capsys.readouterr().out)
        assert payload["interactive_command"] is None
        assert payload["headless_command"] is None

    @patch("agento.framework.agent_view_runtime.resolve_agent_view_runtime")
    @patch("agento.framework.db.get_connection_or_exit")
    @patch("agento.framework.cli.runtime._load_framework_config")
    @patch("agento.framework.workspace.get_agent_view_by_code")
    def test_missing_workspace_exits(
        self, mock_av_lookup, mock_config, mock_get_conn, mock_resolve, capsys,
    ):
        mock_config.return_value = ({}, None, None)
        mock_get_conn.return_value = _mock_conn()
        av = _make_agent_view()
        mock_av_lookup.return_value = av
        mock_resolve.return_value = AgentViewRuntime(
            agent_view=av,
            workspace=None,
            provider="claude",
        )

        with pytest.raises(SystemExit) as exc:
            AgentViewRuntimeCommand().execute(_args())
        assert exc.value.code == 1
        assert "workspace for agent_view 'dev_01' not found" in capsys.readouterr().err

    @patch("agento.framework.agent_view_runtime.resolve_agent_view_runtime")
    @patch("agento.framework.db.get_connection_or_exit")
    @patch("agento.framework.cli.runtime._load_framework_config")
    @patch("agento.framework.workspace.get_agent_view_by_code")
    def test_null_provider_emits_null_commands(
        self, mock_av_lookup, mock_config, mock_get_conn, mock_resolve, capsys,
    ):
        """No provider → no CliInvoker lookup attempted; interactive/headless both null."""
        mock_config.return_value = ({}, None, None)
        mock_get_conn.return_value = _mock_conn()
        av = _make_agent_view()
        mock_av_lookup.return_value = av
        mock_resolve.return_value = AgentViewRuntime(
            agent_view=av,
            workspace=_make_workspace(),
            provider=None,
            model=None,
        )

        AgentViewRuntimeCommand().execute(_args())

        payload = json.loads(capsys.readouterr().out)
        assert payload["provider"] is None
        assert payload["model"] is None
        assert payload["home"] == "/workspace/build/it/dev_01/current"
        assert payload["interactive_command"] is None
        assert payload["headless_command"] is None
