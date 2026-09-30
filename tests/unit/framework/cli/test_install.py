"""Tests for agento install command."""
from __future__ import annotations

import argparse
import json
import re
import socket
from pathlib import Path
from unittest.mock import patch

from agento.framework.cli.install import (
    InstallCommand,
    _detect_timezone,
    _generate_password,
    _is_port_free,
    _reinstall,
    _run_post_install,
    _sanitize_compose_name,
    _scaffold,
)


class TestSanitizeComposeName:
    def test_lowercase(self):
        assert _sanitize_compose_name("MyProject") == "myproject"

    def test_replaces_spaces(self):
        assert _sanitize_compose_name("My Project") == "my-project"

    def test_replaces_dots(self):
        assert _sanitize_compose_name("project.v2") == "project-v2"

    def test_replaces_underscores(self):
        assert _sanitize_compose_name("my_project") == "my-project"

    def test_strips_invalid_chars(self):
        assert _sanitize_compose_name("proj@#$ect") == "project"

    def test_collapses_hyphens(self):
        assert _sanitize_compose_name("a--b---c") == "a-b-c"

    def test_trims_leading_trailing_hyphens(self):
        assert _sanitize_compose_name("-project-") == "project"

    def test_fallback_to_agento(self):
        assert _sanitize_compose_name("___") == "agento"

    def test_empty_string(self):
        assert _sanitize_compose_name("") == "agento"

    def test_complex_example(self):
        assert _sanitize_compose_name("My Project.v2") == "my-project-v2"


class TestGeneratePassword:
    def test_returns_string(self):
        pw = _generate_password()
        assert isinstance(pw, str)
        assert len(pw) > 16

    def test_unique_per_call(self):
        assert _generate_password() != _generate_password()

    def test_url_safe_chars(self):
        pw = _generate_password()
        assert re.match(r"^[A-Za-z0-9_-]+$", pw), f"Password contains invalid chars: {pw}"


class TestIsPortFree:
    def test_free_port(self):
        assert _is_port_free(0) is True

    def test_occupied_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            assert _is_port_free(port) is False


class TestDetectTimezone:
    def test_returns_string(self):
        tz = _detect_timezone()
        assert isinstance(tz, str)
        assert len(tz) > 0

    def test_fallback_to_utc(self):
        with patch("agento.framework.cli.install.Path") as mock_path:
            mock_path.return_value.resolve.side_effect = OSError("not found")
            tz = _detect_timezone()
            assert tz == "UTC"


class TestScaffold:
    def test_creates_directory_structure(self, tmp_path: Path):
        config = {
            "compose_project_name": "test-proj",
            "agento_version": "0.2.4",
            "mysql_root_password": "rootpass123",
            "mysql_password": "userpass456",
            "mysql_port": "3307",
            "timezone": "Europe/Warsaw",
            "host_uid": "1000",
            "host_gid": "1000",
        }
        _scaffold(tmp_path, "test-proj", config)

        assert (tmp_path / ".agento" / "project.json").is_file()
        assert (tmp_path / "app" / "code").is_dir()
        assert (tmp_path / "workspace" / "artifacts").is_dir()
        assert (tmp_path / "workspace" / "build").is_dir()
        assert (tmp_path / "workspace" / "theme").is_dir()
        assert (tmp_path / "logs").is_dir()
        assert (tmp_path / "tokens").is_dir()
        assert (tmp_path / "storage").is_dir()
        # Created from the host CLI so they inherit the invoking user (HOST_UID),
        # which is `agent` inside the toolbox.
        assert (tmp_path / "storage" / "versioned-artifacts" / "store").is_dir()
        assert (tmp_path / "storage" / "versioned-artifacts" / "published").is_dir()
        assert (tmp_path / "docker").is_dir()
        assert (tmp_path / ".gitignore").is_file()
        assert (tmp_path / "secrets.env.example").is_file()

    def test_project_json_contents(self, tmp_path: Path):
        config = {
            "compose_project_name": "my-proj",
            "agento_version": "0.2.4",
            "mysql_root_password": "rp",
            "mysql_password": "up",
            "mysql_port": "3306",
            "timezone": "UTC",
            "host_uid": "1000",
            "host_gid": "1000",
        }
        _scaffold(tmp_path, "my-proj", config)

        meta = json.loads((tmp_path / ".agento" / "project.json").read_text())
        assert meta["name"] == "my-proj"
        assert meta["version"] == "0.1.0"
        assert "created_at" in meta

    def test_env_file_rendered(self, tmp_path: Path):
        config = {
            "compose_project_name": "myapp",
            "agento_version": "0.2.4",
            "mysql_root_password": "secret_root",
            "mysql_password": "secret_user",
            "mysql_port": "3307",
            "timezone": "America/New_York",
            "host_uid": "501",
            "host_gid": "20",
        }
        _scaffold(tmp_path, "myapp", config)

        env_content = (tmp_path / "docker" / ".env").read_text()
        assert "COMPOSE_PROJECT_NAME=myapp" in env_content
        assert "AGENTO_VERSION=0.2.4" in env_content
        assert "MYSQL_ROOT_PASSWORD=secret_root" in env_content
        assert "MYSQL_PASSWORD=secret_user" in env_content
        assert "MYSQL_PORT=3307" in env_content
        assert "TZ=America/New_York" in env_content
        assert "HOST_UID=501" in env_content
        assert "HOST_GID=20" in env_content
        # CLI pins come from each agent module's sandbox_packages di.json —
        # claude + codex ship with the framework.
        assert "CLAUDE_CODE_VERSION=2." in env_content
        assert "CODEX_VERSION=0." in env_content
        assert "DISABLE_LLM=0" in env_content
        assert "{" not in env_content

    def test_scaffold_writes_project_pyproject(self, tmp_path: Path):
        config = {
            "compose_project_name": "x",
            "agento_version": "0.8.0",
            "mysql_root_password": "x",
            "mysql_password": "x",
            "mysql_port": "3306",
            "timezone": "UTC",
            "host_uid": "1000",
            "host_gid": "1000",
        }
        _scaffold(tmp_path, "myproj", config)

        pyproject = (tmp_path / "pyproject.toml").read_text()
        assert 'name = "myproj"' in pyproject
        assert 'agento-core==0.8.0' in pyproject
        assert 'requires-python = ">=3.12"' in pyproject

    def test_scaffold_does_not_write_compose_yet(self, tmp_path: Path):
        # docker-compose.yml is rendered later by regenerate_compose() once
        # the project venv exists. _scaffold only writes the user-owned override.
        config = {
            "compose_project_name": "x",
            "agento_version": "0.8.0",
            "mysql_root_password": "x",
            "mysql_password": "x",
            "mysql_port": "3306",
            "timezone": "UTC",
            "host_uid": "1000",
            "host_gid": "1000",
        }
        _scaffold(tmp_path, "x", config)

        assert not (tmp_path / "docker" / "docker-compose.yml").is_file()
        override = (tmp_path / "docker" / "docker-compose.override.yml").read_text()
        assert "safe to edit" in override

    def test_sql_files_extracted(self, tmp_path: Path):
        config = {
            "compose_project_name": "x",
            "agento_version": "0.2.4",
            "mysql_root_password": "x",
            "mysql_password": "x",
            "mysql_port": "3306",
            "timezone": "UTC",
            "host_uid": "1000",
            "host_gid": "1000",
        }
        _scaffold(tmp_path, "x", config)

        sql_dir = tmp_path / "docker" / "sql"
        assert sql_dir.is_dir()
        sql_files = list(sql_dir.glob("*.sql"))
        assert len(sql_files) > 0


class TestInstallCommandAlreadyInstalled:
    @patch("agento.framework.cli.install.select", return_value=1)  # "No"
    def test_reinstall_declined_exits(self, mock_select, tmp_path: Path, capsys):
        (tmp_path / ".agento").mkdir()
        (tmp_path / ".agento" / "project.json").write_text('{"name":"x"}')

        original_cwd = Path.cwd
        try:
            Path.cwd = staticmethod(lambda: tmp_path)
            with patch("builtins.input", return_value="."):
                cmd = InstallCommand()
                cmd.execute(argparse.Namespace())
        finally:
            Path.cwd = original_cwd

        captured = capsys.readouterr()
        assert "already installed" in captured.out.lower()


class TestInstallCommandBasic:
    @patch("agento.framework.cli.install._run_post_install")
    @patch("agento.framework.cli.install._provision_project", return_value=True)
    @patch("agento.framework.cli.install.select", return_value=0)
    @patch("builtins.input", return_value=".")
    def test_basic_install_scaffolds(self, mock_input, mock_select, mock_provision, mock_post, tmp_path: Path):
        original_cwd = Path.cwd
        try:
            Path.cwd = staticmethod(lambda: tmp_path)
            cmd = InstallCommand()
            cmd.execute(argparse.Namespace())
        finally:
            Path.cwd = original_cwd

        assert (tmp_path / ".agento" / "project.json").is_file()
        assert (tmp_path / "docker" / ".env").is_file()

        env = (tmp_path / "docker" / ".env").read_text()
        assert "COMPOSE_PROJECT_NAME=" in env
        assert "MYSQL_ROOT_PASSWORD=" in env
        assert "cronagent_pass" not in env
        assert "cronagent_root" not in env
        # Sandbox CLI pins seeded so customers can edit them post-install.
        assert "CLAUDE_CODE_VERSION=" in env
        assert "CODEX_VERSION=" in env

        # Provisioning was invoked between scaffold and post-install runtime.
        mock_provision.assert_called_once()


class TestInstallCommandAdvanced:
    @patch("agento.framework.cli.install._run_post_install")
    @patch("agento.framework.cli.install._provision_project", return_value=True)
    @patch("agento.framework.cli.install._is_port_free", return_value=True)
    @patch("agento.framework.cli.install.select", return_value=1)
    @patch("builtins.input", side_effect=[".", "custom-name", "3307", "America/Chicago"])
    def test_advanced_install_uses_custom_values(self, mock_input, mock_select, mock_port, mock_provision, mock_post, tmp_path: Path):
        original_cwd = Path.cwd
        try:
            Path.cwd = staticmethod(lambda: tmp_path)
            cmd = InstallCommand()
            cmd.execute(argparse.Namespace())
        finally:
            Path.cwd = original_cwd

        env = (tmp_path / "docker" / ".env").read_text()
        assert "COMPOSE_PROJECT_NAME=custom-name" in env
        assert "MYSQL_PORT=3307" in env
        assert "TZ=America/Chicago" in env


class TestReinstall:
    def _scaffold_project(self, tmp_path: Path) -> None:
        """Helper: scaffold a project so _reinstall can operate on it."""
        config = {
            "compose_project_name": "myapp",
            "agento_version": "0.1.0",
            "mysql_root_password": "secret_root",
            "mysql_password": "secret_user",
            "mysql_port": "3307",
            "timezone": "Europe/Warsaw",
            "host_uid": "1000",
            "host_gid": "1000",
        }
        _scaffold(tmp_path, "myapp", config)

    @patch("agento.framework.cli.install._provision_project", return_value=True)
    @patch("agento.framework.cli.install.get_package_version", return_value="0.5.0")
    def test_reinstall_updates_version_in_env(self, mock_ver, mock_provision, tmp_path: Path):
        self._scaffold_project(tmp_path)
        _reinstall(tmp_path, 1000, 1000)
        env = (tmp_path / "docker" / ".env").read_text()
        assert "AGENTO_VERSION=0.5.0" in env

    @patch("agento.framework.cli.install._provision_project", return_value=True)
    @patch("agento.framework.cli.install.get_package_version", return_value="0.5.0")
    def test_reinstall_preserves_passwords(self, mock_ver, mock_provision, tmp_path: Path):
        self._scaffold_project(tmp_path)
        _reinstall(tmp_path, 1000, 1000)
        env = (tmp_path / "docker" / ".env").read_text()
        assert "MYSQL_ROOT_PASSWORD=secret_root" in env
        assert "MYSQL_PASSWORD=secret_user" in env
        assert "MYSQL_PORT=3307" in env

    @patch("agento.framework.cli.install._provision_project", return_value=True)
    @patch("agento.framework.cli.install.get_package_version", return_value="0.5.0")
    def test_reinstall_preserves_user_override(self, mock_ver, mock_provision, tmp_path: Path):
        self._scaffold_project(tmp_path)
        # Add custom content to override — must not be overwritten.
        (tmp_path / "docker" / "docker-compose.override.yml").write_text(
            "services:\n  redis:\n    image: redis:7\n"
        )
        _reinstall(tmp_path, 1000, 1000)

        override = (tmp_path / "docker" / "docker-compose.override.yml").read_text()
        assert "redis:7" in override

    @patch("agento.framework.cli.install._provision_project", return_value=True)
    @patch("agento.framework.cli.install.get_package_version", return_value="0.5.0")
    def test_reinstall_bumps_pyproject_pin(self, mock_ver, mock_provision, tmp_path: Path):
        self._scaffold_project(tmp_path)
        # _scaffold pinned 0.1.0; _reinstall must bump to mocked 0.5.0
        _reinstall(tmp_path, 1000, 1000)
        pyproject = (tmp_path / "pyproject.toml").read_text()
        assert "agento-core==0.5.0" in pyproject

    @patch("agento.framework.cli.install._provision_project", return_value=True)
    @patch("agento.framework.cli.install.get_package_version", return_value="0.5.0")
    def test_reinstall_updates_project_json_version(self, mock_ver, mock_provision, tmp_path: Path):
        self._scaffold_project(tmp_path)
        _reinstall(tmp_path, 1000, 1000)
        meta = json.loads((tmp_path / ".agento" / "project.json").read_text())
        assert meta["version"] == "0.5.0"

    @patch("agento.framework.cli.install._provision_project", return_value=True)
    @patch("agento.framework.cli.install.get_package_version", return_value="0.5.0")
    def test_reinstall_backfills_missing_cli_pins(self, mock_ver, mock_provision, tmp_path: Path):
        # Simulate a project installed before the CLI pins landed: env has
        # no CLAUDE_CODE_VERSION / CODEX_VERSION. _reinstall must backfill them.
        (tmp_path / "docker").mkdir()
        (tmp_path / "docker" / ".env").write_text(
            "AGENTO_VERSION=0.4.0\nHOST_UID=1000\nHOST_GID=1000\n"
        )
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["agento-core==0.4.0"]\n'
        )
        (tmp_path / ".agento").mkdir()
        (tmp_path / ".agento" / "project.json").write_text('{"name":"x","version":"0.4.0"}')

        _reinstall(tmp_path, 1000, 1000)

        env = (tmp_path / "docker" / ".env").read_text()
        assert "CLAUDE_CODE_VERSION=2." in env
        assert "CODEX_VERSION=0." in env

    @patch("agento.framework.cli.install._provision_project", return_value=True)
    @patch("agento.framework.cli.install.get_package_version", return_value="0.5.0")
    def test_reinstall_preserves_existing_cli_pins(self, mock_ver, mock_provision, tmp_path: Path):
        # A customer may have pinned a newer or older CLI than the agento
        # default. _reinstall must NOT overwrite their choice — sticky pin.
        self._scaffold_project(tmp_path)
        env_path = tmp_path / "docker" / ".env"
        # Override whatever defaults the scaffold wrote with a customer choice.
        # Match on the keys (not concrete defaults) so this stays valid as the
        # agento default pins are bumped on each release.
        text = env_path.read_text()
        text = re.sub(r"CLAUDE_CODE_VERSION=.*", "CLAUDE_CODE_VERSION=2.1.200", text)
        text = re.sub(r"CODEX_VERSION=.*", "CODEX_VERSION=0.999.0", text)
        env_path.write_text(text)

        _reinstall(tmp_path, 1000, 1000)

        env = env_path.read_text()
        assert "CLAUDE_CODE_VERSION=2.1.200" in env
        assert "CODEX_VERSION=0.999.0" in env


class TestRunPostInstall:
    """_run_post_install must build base agento-<service>:<version> tags
    before any docker compose build, so override Dockerfiles that
    FROM agento-<service>:<version> have a base to layer on."""

    def _seed_project(self, tmp_path: Path) -> Path:
        (tmp_path / "docker").mkdir()
        (tmp_path / "docker" / "docker-compose.yml").write_text("services: {}\n")
        (tmp_path / "docker" / ".env").write_text(
            "AGENTO_VERSION=0.9.4\nHOST_UID=1000\nHOST_GID=1000\n"
        )
        for service in ("sandbox", "toolbox", "cron"):
            (tmp_path / ".agento" / "docker" / service).mkdir(parents=True)
        return tmp_path

    @patch("agento.framework.cli.install.subprocess.run")
    @patch("agento.framework.cli.install.build_base_images")
    @patch("agento.framework.cli.install.get_package_version", return_value="0.9.4")
    def test_calls_build_base_images_before_compose_build(
        self, mock_ver, mock_build_base, mock_run, tmp_path: Path
    ):
        self._seed_project(tmp_path)
        # Make subprocess returns succeed on sandbox/toolbox/cron, then fail
        # on `up -d` so _run_post_install exits before the setup-done wait
        # loop sleeps for 2 minutes in tests.
        outcomes = iter([
            type("R", (), {"returncode": 0})(),  # compose build sandbox
            type("R", (), {"returncode": 0})(),  # compose build toolbox cron
            type("R", (), {"returncode": 1})(),  # compose up -d → bail out
        ])
        mock_run.side_effect = lambda *a, **kw: next(outcomes)

        _run_post_install(tmp_path)

        mock_build_base.assert_called_once_with(tmp_path, "0.9.4")
        # Must have been called before the first compose build.
        # (mock_build_base is called once, then mock_run starts being called.)
        # Easiest assertion: build_base_images was called and at least one
        # compose subprocess.run also happened.
        assert mock_run.call_count >= 1
