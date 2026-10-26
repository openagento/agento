"""Tests for agento doctor checks."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from agento.framework.cli._project import EXEC_SERVICE
from agento.framework.cli.doctor import (
    _check_binary,
    _check_docker_compose,
    _check_python,
    _check_sandbox_cli_pin,
)


class TestCheckPython:
    def test_current_python_is_ok(self):
        ok, info = _check_python()
        assert ok is True
        assert f"{sys.version_info.major}.{sys.version_info.minor}" in info


class TestCheckBinary:
    def test_found_binary(self):
        # python3 should always be available in test env
        ok, info = _check_binary("python3")
        assert ok is True
        assert info != "not found"

    def test_missing_binary(self):
        ok, info = _check_binary("nonexistent_binary_xyz_12345")
        assert ok is False
        assert info == "not found"


class TestCheckDockerCompose:
    def test_returns_tuple(self):
        ok, info = _check_docker_compose()
        assert isinstance(ok, bool)
        assert isinstance(info, str)


class TestCheckSandboxCliPin:
    def _seed_project(self, tmp_path: Path, env_text: str) -> Path:
        (tmp_path / "docker").mkdir()
        (tmp_path / "docker" / ".env").write_text(env_text)
        (tmp_path / "docker" / "docker-compose.yml").write_text("services: {}\n")
        return tmp_path

    def test_no_pin_in_env_reports_ok(self, tmp_path: Path):
        proj = self._seed_project(tmp_path, "AGENTO_VERSION=0.9.5\n")
        ok, info = _check_sandbox_cli_pin(
            proj, binary="claude", pin_key="CLAUDE_CODE_VERSION",
        )
        assert ok is True
        assert "not set" in info

    @patch("agento.framework.cli.doctor.subprocess.run")
    def test_in_range_reports_ok(self, mock_run, tmp_path: Path):
        proj = self._seed_project(tmp_path, "CLAUDE_CODE_VERSION=~2.1.142\n")
        # Live version is within the tilde window (>=2.1.142 <2.2.0).
        mock_run.return_value = type(
            "R", (), {"returncode": 0, "stdout": "2.1.150 (Claude Code)", "stderr": ""},
        )()
        ok, info = _check_sandbox_cli_pin(
            proj, binary="claude", pin_key="CLAUDE_CODE_VERSION",
        )
        assert ok is True
        assert "2.1.150" in info
        assert "~2.1.142" in info
        # The runner runs the agent CLIs; there is no sandbox service to exec into.
        assert mock_run.call_args.args[0][-3:] == [EXEC_SERVICE, "claude", "--version"]

    @patch("agento.framework.cli.doctor.subprocess.run")
    def test_below_floor_reports_drift(self, mock_run, tmp_path: Path):
        proj = self._seed_project(tmp_path, "CLAUDE_CODE_VERSION=~2.1.142\n")
        # Live version is older than the floor — drift; needs rebuild.
        mock_run.return_value = type(
            "R", (), {"returncode": 0, "stdout": "2.1.100 (Claude Code)", "stderr": ""},
        )()
        ok, info = _check_sandbox_cli_pin(
            proj, binary="claude", pin_key="CLAUDE_CODE_VERSION",
        )
        assert ok is False
        assert "rebuild" in info.lower()
        assert "agento upgrade" in info
        assert "build sandbox" not in info

    @patch("agento.framework.cli.doctor.subprocess.run")
    def test_minor_jump_reports_drift(self, mock_run, tmp_path: Path):
        proj = self._seed_project(tmp_path, "CLAUDE_CODE_VERSION=~2.1.142\n")
        # Live version crossed the minor — tilde forbids that.
        mock_run.return_value = type(
            "R", (), {"returncode": 0, "stdout": "2.2.0 (Claude Code)", "stderr": ""},
        )()
        ok, info = _check_sandbox_cli_pin(
            proj, binary="claude", pin_key="CLAUDE_CODE_VERSION",
        )
        assert ok is False
        assert "2.2.0" in info

    @patch("agento.framework.cli.doctor.subprocess.run")
    def test_runner_not_running_reports_ok(self, mock_run, tmp_path: Path):
        proj = self._seed_project(tmp_path, "CLAUDE_CODE_VERSION=~2.1.142\n")
        # docker compose exec returns non-zero when the container is down.
        mock_run.return_value = type(
            "R", (), {"returncode": 1, "stdout": "", "stderr": "no such service"},
        )()
        ok, info = _check_sandbox_cli_pin(
            proj, binary="claude", pin_key="CLAUDE_CODE_VERSION",
        )
        assert ok is True
        assert "not running" in info

    def test_no_compose_file_reports_ok(self, tmp_path: Path):
        (tmp_path / "docker").mkdir()
        (tmp_path / "docker" / ".env").write_text("CLAUDE_CODE_VERSION=~2.1.142\n")
        # No compose file at all → nothing to exec against.
        ok, info = _check_sandbox_cli_pin(
            tmp_path, binary="claude", pin_key="CLAUDE_CODE_VERSION",
        )
        assert ok is True
        assert "not built yet" in info

    @patch("agento.framework.cli.doctor.subprocess.run", side_effect=FileNotFoundError())
    def test_docker_missing_reports_ok(self, mock_run, tmp_path: Path):
        proj = self._seed_project(tmp_path, "CLAUDE_CODE_VERSION=~2.1.142\n")
        ok, info = _check_sandbox_cli_pin(
            proj, binary="claude", pin_key="CLAUDE_CODE_VERSION",
        )
        assert ok is True
        assert "not running" in info
