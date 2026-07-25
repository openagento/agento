"""Tests for ``agent_view:prepare-run`` — the cron-side command `agento run`
calls to do the same token-pool + materialization the consumer does, then
returns home/working_dir/env/command so the host can ``docker exec sandbox``.

The critical security guarantee: any API-key value the command resolves
appears in the JSON ``env`` field, never anywhere in ``command`` (so the
host can inject via name-only ``-e`` and the secret never hits argv/``ps``).
"""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import pytest


def _make_args(agent_view_code="dev", prompt=None, model=None, yolo=False):
    return argparse.Namespace(
        agent_view_code=agent_view_code, prompt=prompt, model=model, yolo=yolo,
    )


@pytest.fixture
def runtime_stub():
    rt = MagicMock()
    rt.provider = "claude"
    rt.model = None
    rt.workspace = MagicMock(id=3, code="acme")
    rt.agent_view = MagicMock(id=7, code="dev")
    return rt


@pytest.fixture
def token_stub():
    tok = MagicMock()
    tok.id = 42
    tok.credentials = {"api_key": "sk-ant-SECRET"}
    return tok


@pytest.fixture
def invoker_stub():
    inv = MagicMock()
    inv.interactive_command.return_value = ["claude"]
    inv.headless_command.return_value = [
        "claude", "-p", "hello",
        "--dangerously-skip-permissions",
        "--output-format", "stream-json", "--verbose",
    ]
    return inv


@pytest.fixture
def writer_stub():
    w = MagicMock()
    w.credential_env.return_value = {"ANTHROPIC_API_KEY": "sk-ant-SECRET"}
    return w


def _run_command(args, runtime, token, invoker, writer, home, working_dir, *, return_mocks=False):
    """Drive the command's ``execute`` with all heavy deps mocked."""
    from agento.modules.agent_view.src.commands.prepare_run import (
        AgentViewPrepareRunCommand,
    )

    cmd = AgentViewPrepareRunCommand()
    with patch(
        "agento.framework.cli.runtime._load_framework_config",
        return_value=(MagicMock(), MagicMock(), MagicMock()),
    ), patch(
        "agento.framework.db.get_connection_or_exit", return_value=MagicMock(),
    ), patch(
        "agento.framework.workspace.get_agent_view_by_code",
        return_value=MagicMock(id=runtime.agent_view.id, code=runtime.agent_view.code),
    ), patch(
        "agento.framework.agent_view_runtime.resolve_agent_view_runtime",
        return_value=runtime,
    ), patch(
        "agento.framework.agent_manager.token_resolver.TokenResolver",
    ) as MockResolver, patch(
        "agento.framework.run_preparation.materialize_run_workspace",
        return_value=(home, working_dir),
    ) as mock_materialize, patch(
        "agento.framework.config_writer.get_config_writer", return_value=writer,
    ), patch(
        "agento.framework.cli_invoker.get_cli_invoker", return_value=invoker,
    ):
        MockResolver.return_value.resolve.return_value = token
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd.execute(args)
        payload = json.loads(buf.getvalue())
        if return_mocks:
            return payload, mock_materialize
        return payload


class TestAgentViewPrepareRunCommand:
    def test_name_and_help(self):
        from agento.modules.agent_view.src.commands.prepare_run import (
            AgentViewPrepareRunCommand,
        )
        cmd = AgentViewPrepareRunCommand()
        assert cmd.name == "agent_view:prepare-run"

    def test_interactive_payload_shape(
        self, tmp_path, runtime_stub, token_stub, invoker_stub, writer_stub,
    ):
        payload = _run_command(
            _make_args(), runtime_stub, token_stub, invoker_stub, writer_stub,
            home=tmp_path / "artifacts", working_dir=tmp_path / "artifacts",
        )
        assert payload["provider"] == "claude"
        assert payload["agent_view_code"] == "dev"
        assert payload["workspace_code"] == "acme"
        assert payload["home"] == str(tmp_path / "artifacts")
        assert payload["working_dir"] == str(tmp_path / "artifacts")
        assert payload["command"] == ["claude"]
        assert payload["env"] == {"ANTHROPIC_API_KEY": "sk-ant-SECRET"}
        assert payload["token_id"] == 42
        invoker_stub.interactive_command.assert_called_once_with(yolo=False)

    def test_yolo_flag_forwarded_to_interactive_command(
        self, tmp_path, runtime_stub, token_stub, invoker_stub, writer_stub,
    ):
        _run_command(
            _make_args(yolo=True), runtime_stub, token_stub, invoker_stub, writer_stub,
            home=tmp_path / "artifacts", working_dir=tmp_path / "artifacts",
        )
        invoker_stub.interactive_command.assert_called_once_with(yolo=True)

    def test_headless_payload_includes_prompt_command(
        self, tmp_path, runtime_stub, token_stub, invoker_stub, writer_stub,
    ):
        payload = _run_command(
            _make_args(prompt="hello"), runtime_stub, token_stub, invoker_stub, writer_stub,
            home=tmp_path / "artifacts", working_dir=tmp_path / "artifacts",
        )
        assert payload["command"][0] == "claude"
        assert "hello" in payload["command"]
        invoker_stub.headless_command.assert_called_once_with("hello", model=None)

    def test_secret_never_in_command(
        self, tmp_path, runtime_stub, token_stub, invoker_stub, writer_stub,
    ):
        """The exact contract the host depends on: secret in ``env``, not in ``command``."""
        payload = _run_command(
            _make_args(prompt="hello"), runtime_stub, token_stub, invoker_stub, writer_stub,
            home=tmp_path / "artifacts", working_dir=tmp_path / "artifacts",
        )
        flat_command = " ".join(payload["command"])
        assert "sk-ant-SECRET" not in flat_command
        assert payload["env"]["ANTHROPIC_API_KEY"] == "sk-ant-SECRET"

    def test_materializes_with_resolved_token(
        self, tmp_path, runtime_stub, token_stub, invoker_stub, writer_stub,
    ):
        payload, mock_materialize = _run_command(
            _make_args(), runtime_stub, token_stub, invoker_stub, writer_stub,
            home=tmp_path / "artifacts", working_dir=tmp_path / "artifacts",
            return_mocks=True,
        )

        assert payload["home"] == str(tmp_path / "artifacts")
        mock_materialize.assert_called_once()
        assert mock_materialize.call_args.kwargs["token"] is token_stub
        run_id = mock_materialize.call_args.kwargs["run_id"]
        assert isinstance(run_id, str)
        assert run_id.startswith("run-")
        assert run_id != "run"

    def test_missing_cli_invoker_returns_null_command(
        self, tmp_path, runtime_stub, token_stub, writer_stub,
    ):
        """When no CliInvoker is registered, ``command`` MUST be ``None`` in
        the JSON payload (mirrors ``agent_view:runtime``) so the host
        ``RunCommand`` shows its actionable "no CliInvoker registered" error
        instead of cron raising a traceback.
        """
        from agento.modules.agent_view.src.commands.prepare_run import (
            AgentViewPrepareRunCommand,
        )

        cmd = AgentViewPrepareRunCommand()
        with patch(
            "agento.framework.cli.runtime._load_framework_config",
            return_value=(MagicMock(), MagicMock(), MagicMock()),
        ), patch(
            "agento.framework.db.get_connection_or_exit", return_value=MagicMock(),
        ), patch(
            "agento.framework.workspace.get_agent_view_by_code",
            return_value=MagicMock(id=7, code="dev"),
        ), patch(
            "agento.framework.agent_view_runtime.resolve_agent_view_runtime",
            return_value=runtime_stub,
        ), patch(
            "agento.framework.agent_manager.token_resolver.TokenResolver",
        ) as MockResolver, patch(
            "agento.framework.run_preparation.materialize_run_workspace",
            return_value=(tmp_path / "artifacts", tmp_path / "artifacts"),
        ), patch(
            "agento.framework.config_writer.get_config_writer", return_value=writer_stub,
        ), patch(
            "agento.framework.cli_invoker.get_cli_invoker",
            side_effect=ValueError("No CliInvoker registered for provider 'exotic'"),
        ):
            MockResolver.return_value.resolve.return_value = token_stub
            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd.execute(_make_args())
            payload = json.loads(buf.getvalue())

        assert payload["command"] is None
        # provider + env still resolved — the host can show its actionable
        # error message because the cron-side path didn't crash.
        assert payload["provider"] == "claude"
        assert payload["env"] == {"ANTHROPIC_API_KEY": "sk-ant-SECRET"}
