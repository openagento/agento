"""Tests for `agento run <agent_view_code> [prompt]` — RunCommand."""
from __future__ import annotations

import argparse
import json
import subprocess
from unittest.mock import patch

import pytest

from agento.framework.cli.run import RunCommand

_INTERACTIVE_CLAUDE = ["claude"]
_HEADLESS_CLAUDE = [
    "claude", "-p", "jakie masz toole z mcp i skille?",
    "--dangerously-skip-permissions",
    "--output-format", "stream-json",
    "--verbose",
    "--model", "claude-opus-4-6",
]
_INTERACTIVE_CODEX = ["codex"]
_HEADLESS_CODEX = [
    "codex", "exec", "jakie masz toole z mcp i skille?",
    "--dangerously-bypass-approvals-and-sandbox",
    "--skip-git-repo-check",
    "--model", "gpt-5.4",
]


def _base_runtime(harness="claude", model="claude-opus-4-6", *, prompt=False):
    """Payload from the cron-side ``agent_view:prepare-run`` command.

    Mirrors :class:`AgentViewPrepareRunCommand`'s JSON output: unified
    ``command`` (interactive or headless depending on whether a prompt was
    passed), a per-run artifacts ``home``/``working_dir``, and an ``env`` dict
    that's empty when no API-key credential delivery is needed.
    """
    interactive = _INTERACTIVE_CLAUDE if harness == "claude" else _INTERACTIVE_CODEX
    headless = _HEADLESS_CLAUDE if harness == "claude" else _HEADLESS_CODEX
    return {
        "agent_view_id": 2,
        "agent_view_code": "dev_01",
        "workspace_id": 1,
        "workspace_code": "it",
        "harness": harness,
        "model": model,
        "home": "/workspace/artifacts/it/dev_01/run",
        "working_dir": "/workspace/artifacts/it/dev_01/run",
        "command": headless if prompt else interactive,
        "env": {},
        "credential_id": 99,
    }


def _make_args(code="dev_01", *, prompt=None, yolo=False):
    return argparse.Namespace(
        agent_view_code=code, prompt=list(prompt or []), yolo=yolo,
    )


def _make_prompt_args(code="dev_01", prompt="jakie masz toole z mcp i skille?"):
    return argparse.Namespace(agent_view_code=code, prompt=[prompt], yolo=False)


def _project_layout(tmp_path, *, include_current=True):
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    compose = docker_dir / "docker-compose.yml"
    compose.write_text("name: agento\nservices: {}\n")
    (tmp_path / ".agento").mkdir()
    (tmp_path / ".agento" / "project.json").write_text("{}")
    if include_current:
        build_root = tmp_path / "workspace" / "build" / "it" / "dev_01"
        build_root.mkdir(parents=True)
        (build_root / "builds").mkdir()
        (build_root / "current").symlink_to(build_root / "builds")
    return tmp_path, compose


class TestRunCommand:
    def test_properties(self):
        cmd = RunCommand()
        assert cmd.name == "run"
        assert cmd.shortcut == "ru"

    def test_missing_project_root_exits(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=None),
            pytest.raises(SystemExit) as exc,
        ):
            RunCommand().execute(_make_args())
        assert exc.value.code == 1
        assert "not inside an agento project" in capsys.readouterr().err

    def test_missing_compose_file_exits(self, tmp_path, capsys):
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=tmp_path),
            patch("agento.framework.cli.run.compose_file_flags", return_value=[]),
            pytest.raises(SystemExit) as exc,
        ):
            RunCommand().execute(_make_args())
        assert exc.value.code == 1
        assert "docker-compose.yml not found" in capsys.readouterr().err

    def test_missing_harness_exits_with_hint(self, tmp_path, capsys):
        project_root, compose = _project_layout(tmp_path)
        runtime = {**_base_runtime(), "harness": None, "command": None}
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch("agento.framework.cli.run._fetch_runtime", return_value=runtime),
            pytest.raises(SystemExit) as exc,
        ):
            RunCommand().execute(_make_args())
        err = capsys.readouterr().err
        assert exc.value.code == 1
        assert "no harness configured" in err
        assert "config:set agent_view/harness" in err

    def test_unregistered_harness_exits(self, tmp_path, capsys):
        project_root, compose = _project_layout(tmp_path)
        # Cron returns a harness but the unified ``command`` is null when that
        # harness isn't registered (so no CommandBuilder could build one).
        runtime = {**_base_runtime(harness="exotic"), "command": None}
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch("agento.framework.cli.run._fetch_runtime", return_value=runtime),
            pytest.raises(SystemExit) as exc,
        ):
            RunCommand().execute(_make_args())
        err = capsys.readouterr().err
        assert exc.value.code == 1
        assert "is not registered" in err

    def test_missing_current_build_exits_with_hint(self, tmp_path, capsys):
        project_root, compose = _project_layout(tmp_path, include_current=False)
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch("agento.framework.cli.run._fetch_runtime", return_value=_base_runtime()),
            pytest.raises(SystemExit) as exc,
        ):
            RunCommand().execute(_make_args())
        err = capsys.readouterr().err
        assert exc.value.code == 1
        assert "no build found" in err
        assert "workspace:build --agent-view dev_01" in err

    def test_interactive_happy_path_uses_returned_command(self, tmp_path):
        project_root, compose = _project_layout(tmp_path)
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch("agento.framework.cli.run._fetch_runtime", return_value=_base_runtime()),
            patch("agento.framework.cli.run.os.execvp") as mock_execvp,
        ):
            RunCommand().execute(_make_args())
        mock_execvp.assert_called_once()
        program, argv = mock_execvp.call_args.args
        assert program == "docker"
        assert argv[:4] == ["docker", "compose", "-f", str(compose)]
        assert "exec" in argv
        assert "-it" in argv
        assert "sandbox" in argv
        assert argv[-1] == "claude"
        assert "HOME=/workspace/artifacts/it/dev_01/run" in argv
        # cwd is the per-run artifacts dir (mirrors the consumer's job layout).
        assert argv[argv.index("-w") + 1] == "/workspace/artifacts/it/dev_01/run"

    def test_interactive_for_codex_uses_codex_binary(self, tmp_path):
        project_root, compose = _project_layout(tmp_path)
        runtime = _base_runtime(harness="codex", model="gpt-5.4")
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch("agento.framework.cli.run._fetch_runtime", return_value=runtime),
            patch("agento.framework.cli.run.os.execvp") as mock_execvp,
        ):
            RunCommand().execute(_make_args())
        _, argv = mock_execvp.call_args.args
        assert argv[-1] == "codex"

    def test_yolo_flag_is_forwarded_to_prepare_run(self, tmp_path):
        project_root, compose = _project_layout(tmp_path)
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch(
                "agento.framework.cli.run._fetch_runtime", return_value=_base_runtime(),
            ) as mock_fetch,
            patch("agento.framework.cli.run.os.execvp"),
        ):
            RunCommand().execute(_make_args(yolo=True))
        assert mock_fetch.call_args.kwargs["yolo"] is True

    def test_trailing_yolo_token_is_recovered_from_remainder(self, tmp_path):
        # `agento run dev --yolo` — argparse.REMAINDER swallows --yolo into `prompt`;
        # it must still be treated as the flag (interactive), not a headless prompt.
        project_root, compose = _project_layout(tmp_path)
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch(
                "agento.framework.cli.run._fetch_runtime", return_value=_base_runtime(),
            ) as mock_fetch,
            patch("agento.framework.cli.run.os.execvp"),
        ):
            RunCommand().execute(_make_args(prompt=["--yolo"]))
        assert mock_fetch.call_args.kwargs["yolo"] is True
        assert mock_fetch.call_args.kwargs["prompt"] == ""

    def test_headless_claude_uses_headless_command_from_runtime(self, tmp_path):
        project_root, compose = _project_layout(tmp_path)
        runtime = _base_runtime(prompt=True)
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch("agento.framework.cli.run._fetch_runtime", return_value=runtime) as mock_fetch,
            patch("agento.framework.cli.run.subprocess.run", return_value=completed) as mock_run,
            pytest.raises(SystemExit) as exc,
        ):
            RunCommand().execute(_make_prompt_args())
        assert exc.value.code == 0
        # _fetch_runtime was called with the prompt so cron can build the headless command
        assert mock_fetch.call_args.kwargs["prompt"] == "jakie masz toole z mcp i skille?"

        argv = mock_run.call_args.args[0]
        assert "-T" in argv and "-it" not in argv
        # After "sandbox" comes the ssh-prelude wrapper: sh -c <script> -- <cmd...>
        idx = argv.index("sandbox") + 1
        assert argv[idx:idx + 2] == ["sh", "-c"]
        assert argv[idx + 3] == "--"
        assert argv[idx + 4:] == _HEADLESS_CLAUDE
        assert mock_run.call_args.kwargs["stdin"] == subprocess.DEVNULL

    def test_headless_codex_uses_headless_command_from_runtime(self, tmp_path):
        project_root, compose = _project_layout(tmp_path)
        runtime = _base_runtime(harness="codex", model="gpt-5.4", prompt=True)
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch("agento.framework.cli.run._fetch_runtime", return_value=runtime),
            patch("agento.framework.cli.run.subprocess.run", return_value=completed) as mock_run,
            pytest.raises(SystemExit) as exc,
        ):
            RunCommand().execute(_make_prompt_args())
        assert exc.value.code == 0
        argv = mock_run.call_args.args[0]
        idx = argv.index("sandbox") + 1
        assert argv[idx:idx + 2] == ["sh", "-c"]
        assert argv[idx + 4:] == _HEADLESS_CODEX

    def test_interactive_runs_as_agent_user(self, tmp_path):
        project_root, compose = _project_layout(tmp_path)
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch("agento.framework.cli.run._fetch_runtime", return_value=_base_runtime()),
            patch("agento.framework.cli.run.os.execvp") as mock_execvp,
        ):
            RunCommand().execute(_make_args())
        _, argv = mock_execvp.call_args.args
        u_idx = argv.index("-u")
        assert argv[u_idx + 1] == "agent"
        assert argv.index("-u") < argv.index("sandbox")

    def test_headless_runs_as_agent_user(self, tmp_path):
        project_root, compose = _project_layout(tmp_path)
        runtime = _base_runtime(prompt=True)
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch("agento.framework.cli.run._fetch_runtime", return_value=runtime),
            patch("agento.framework.cli.run.subprocess.run", return_value=completed) as mock_run,
            pytest.raises(SystemExit),
        ):
            RunCommand().execute(_make_prompt_args())
        argv = mock_run.call_args.args[0]
        u_idx = argv.index("-u")
        assert argv[u_idx + 1] == "agent"
        assert argv.index("-u") < argv.index("sandbox")

    def test_headless_propagates_exit_code(self, tmp_path):
        project_root, compose = _project_layout(tmp_path)
        runtime = _base_runtime(harness="codex", model="gpt-5.4", prompt=True)
        completed = subprocess.CompletedProcess(args=[], returncode=17)
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch("agento.framework.cli.run._fetch_runtime", return_value=runtime),
            patch("agento.framework.cli.run.subprocess.run", return_value=completed),
            pytest.raises(SystemExit) as exc,
        ):
            RunCommand().execute(_make_prompt_args())
        assert exc.value.code == 17


class TestFetchRuntime:
    def test_parses_cron_json(self, tmp_path):
        from agento.framework.cli.run import _fetch_runtime

        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps(_base_runtime()) + "\n",
            stderr="",
        )
        with patch("agento.framework.cli.run.subprocess.run", return_value=fake_result):
            result = _fetch_runtime(["-f", str(tmp_path / "compose.yml")],"dev_01")
        assert result["command"] == _INTERACTIVE_CLAUDE
        assert result["env"] == {}
        assert result["working_dir"] == "/workspace/artifacts/it/dev_01/run"

    def test_calls_prepare_run_subcommand(self, tmp_path):
        """The host must call ``agent_view:prepare-run`` (not the read-only
        ``agent_view:runtime``) so the token pool's ``used_at`` is stamped
        and credentials get materialized like a real job."""
        from agento.framework.cli.run import _fetch_runtime

        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps(_base_runtime()) + "\n", stderr="",
        )
        with patch(
            "agento.framework.cli.run.subprocess.run", return_value=fake_result,
        ) as mock_run:
            _fetch_runtime(["-f", str(tmp_path / "compose.yml")], "dev_01")
        cmd = mock_run.call_args.args[0]
        assert "agent_view:prepare-run" in cmd
        assert "agent_view:runtime" not in cmd

    def test_passes_prompt_flag_to_container(self, tmp_path):
        from agento.framework.cli.run import _fetch_runtime

        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps(_base_runtime(prompt=True)) + "\n",
            stderr="",
        )
        with patch(
            "agento.framework.cli.run.subprocess.run", return_value=fake_result,
        ) as mock_run:
            _fetch_runtime(["-f", str(tmp_path / "compose.yml")],"dev_01", prompt="hello")
        cmd = mock_run.call_args.args[0]
        assert "--prompt" in cmd
        assert cmd[cmd.index("--prompt") + 1] == "hello"

    def test_propagates_nonzero_exit(self, tmp_path, capsys):
        from agento.framework.cli.run import _fetch_runtime

        fake_result = subprocess.CompletedProcess(
            args=[], returncode=1,
            stdout="", stderr="Error: agent_view 'missing' not found\n",
        )
        with (
            patch("agento.framework.cli.run.subprocess.run", return_value=fake_result),
            pytest.raises(SystemExit) as exc,
        ):
            _fetch_runtime(["-f", str(tmp_path / "compose.yml")],"missing")
        assert exc.value.code == 1
        assert "agent_view 'missing' not found" in capsys.readouterr().err

    def test_bad_json_exits(self, tmp_path, capsys):
        from agento.framework.cli.run import _fetch_runtime

        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="not json at all", stderr="",
        )
        with (
            patch("agento.framework.cli.run.subprocess.run", return_value=fake_result),
            pytest.raises(SystemExit) as exc,
        ):
            _fetch_runtime(["-f", str(tmp_path / "compose.yml")],"dev_01")
        assert exc.value.code == 1
        assert "could not parse runtime JSON" in capsys.readouterr().err

    def test_bad_json_does_not_leak_stdout_to_stderr(self, tmp_path, capsys):
        """If parsing fails, the raw stdout MUST NOT be echoed — it may carry
        the API-key value from ``prepare-run``'s ``env`` field if cron emitted
        a stray warning that broke JSON parsing.
        """
        from agento.framework.cli.run import _fetch_runtime

        # Realistic failure mode: a stray warning prefix sneaks in before
        # the JSON payload, which itself carries the secret in env.
        leaky_stdout = (
            'WARNING: deprecated thing\n'
            '{"harness": "claude", "env": {"ANTHROPIC_API_KEY": "sk-ant-SECRET"}}\n'
        )
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=leaky_stdout, stderr="",
        )
        with (
            patch("agento.framework.cli.run.subprocess.run", return_value=fake_result),
            pytest.raises(SystemExit),
        ):
            _fetch_runtime(["-f", str(tmp_path / "compose.yml")], "dev_01")
        err = capsys.readouterr().err
        assert "sk-ant-SECRET" not in err
        assert "could not parse runtime JSON" in err


# ---------------------------------------------------------------------------
# New contract: cli/run.py calls `agent_view:prepare-run`, which returns a
# unified `command` plus a `working_dir` (per-run artifacts) and an `env` dict
# whose values must be injected via docker's NAME-ONLY ``-e KEY`` form so the
# secret value never appears in argv/``ps`` (matches the 1ccb38a stdin-only
# secrets stance for credential:register).
# ---------------------------------------------------------------------------


def _prepare_runtime(harness="claude", model="claude-opus-4-6", *, prompt=False, env=None):
    return {
        "agent_view_id": 2,
        "agent_view_code": "dev_01",
        "workspace_id": 1,
        "workspace_code": "it",
        "harness": harness,
        "model": model,
        "home": "/workspace/artifacts/it/dev_01/run",
        "working_dir": "/workspace/artifacts/it/dev_01/run",
        "command": (
            (_HEADLESS_CLAUDE if harness == "claude" else _HEADLESS_CODEX)
            if prompt else
            (_INTERACTIVE_CLAUDE if harness == "claude" else _INTERACTIVE_CODEX)
        ),
        "env": env or {},
        "credential_id": 99,
    }


class TestRunCommandEnvInjection:
    """The host must pass docker `-e KEY` (no value) for each env entry and
    place the value in the child-process environment — argv must stay clean.
    """

    def test_headless_passes_name_only_e_flag_and_value_via_env(self, tmp_path):
        project_root, compose = _project_layout(tmp_path)
        runtime = _prepare_runtime(prompt=True, env={"ANTHROPIC_API_KEY": "sk-ant-SECRET"})
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch("agento.framework.cli.run._fetch_runtime", return_value=runtime),
            patch("agento.framework.cli.run.subprocess.run", return_value=completed) as mock_run,
            pytest.raises(SystemExit),
        ):
            RunCommand().execute(_make_prompt_args())

        argv = mock_run.call_args.args[0]
        # Name-only -e KEY appears, but the SECRET value never appears as argv.
        e_positions = [i for i, a in enumerate(argv) if a == "-e"]
        name_only_keys = [argv[i + 1] for i in e_positions if "=" not in argv[i + 1]]
        assert "ANTHROPIC_API_KEY" in name_only_keys
        assert "sk-ant-SECRET" not in argv
        for a in argv:
            assert "sk-ant-SECRET" not in a

        # Value goes via the child process env so docker reads it from the parent.
        env_passed = mock_run.call_args.kwargs.get("env") or {}
        assert env_passed.get("ANTHROPIC_API_KEY") == "sk-ant-SECRET"

    def test_interactive_sets_env_in_os_environ_and_uses_name_only_e(self, tmp_path, monkeypatch):
        project_root, compose = _project_layout(tmp_path)
        runtime = _prepare_runtime(env={"ANTHROPIC_API_KEY": "sk-ant-SECRET"})
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch("agento.framework.cli.run._fetch_runtime", return_value=runtime),
            patch("agento.framework.cli.run.os.execvp") as mock_execvp,
        ):
            RunCommand().execute(_make_args())

        _, argv = mock_execvp.call_args.args
        e_positions = [i for i, a in enumerate(argv) if a == "-e"]
        name_only_keys = [argv[i + 1] for i in e_positions if "=" not in argv[i + 1]]
        assert "ANTHROPIC_API_KEY" in name_only_keys
        for a in argv:
            assert "sk-ant-SECRET" not in a
        # For execvp the child inherits os.environ — value must be set there.
        import os as _os
        assert _os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-SECRET"

    def test_working_dir_and_home_use_artifacts_dir(self, tmp_path):
        project_root, compose = _project_layout(tmp_path)
        runtime = _prepare_runtime(prompt=True)
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch("agento.framework.cli.run._fetch_runtime", return_value=runtime),
            patch("agento.framework.cli.run.subprocess.run", return_value=completed) as mock_run,
            pytest.raises(SystemExit),
        ):
            RunCommand().execute(_make_prompt_args())
        argv = mock_run.call_args.args[0]
        # Both HOME and cwd should point at the selected run's artifacts dir.
        assert argv[argv.index("-w") + 1] == "/workspace/artifacts/it/dev_01/run"
        assert "HOME=/workspace/artifacts/it/dev_01/run" in argv

    def test_empty_env_dict_adds_no_extra_e_flag(self, tmp_path):
        project_root, compose = _project_layout(tmp_path)
        runtime = _prepare_runtime(env={})
        with (
            patch("agento.framework.cli.run.find_project_root", return_value=project_root),
            patch("agento.framework.cli.run.compose_file_flags", return_value=["-f", str(compose)]),
            patch("agento.framework.cli.run._fetch_runtime", return_value=runtime),
            patch("agento.framework.cli.run.os.execvp") as mock_execvp,
        ):
            RunCommand().execute(_make_args())
        _, argv = mock_execvp.call_args.args
        # Only the framework's own -e KEY=VAL entries (HOME, TERM, COLORTERM) appear.
        e_positions = [i for i, a in enumerate(argv) if a == "-e"]
        name_only = [argv[i + 1] for i in e_positions if "=" not in argv[i + 1]]
        assert name_only == []
