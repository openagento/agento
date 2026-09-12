"""Tests for agento upgrade command."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agento.framework.cli._project import update_dotenv_value


class TestUpgradeUpdatesEnv:
    def test_updates_version(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text("AGENTO_VERSION=0.2.0\nMYSQL_PASSWORD=secret\n")
        update_dotenv_value(env, "AGENTO_VERSION", "0.3.0")
        content = env.read_text()
        assert "AGENTO_VERSION=0.3.0\n" in content
        assert "MYSQL_PASSWORD=secret\n" in content

    def test_specific_version(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text("AGENTO_VERSION=0.2.0\n")
        update_dotenv_value(env, "AGENTO_VERSION", "0.4.0")
        assert "AGENTO_VERSION=0.4.0\n" in env.read_text()


class TestUpgradeCommand:
    def _setup_project(self, tmp_path: Path) -> Path:
        """Create a minimal project layout for upgrade tests."""
        (tmp_path / "docker").mkdir()
        env = tmp_path / "docker" / ".env"
        env.write_text("AGENTO_VERSION=0.2.0\n")
        return env

    @patch("agento.framework.cli.upgrade.regenerate_compose")
    @patch("agento.framework.cli.upgrade.materialize_docker_context")
    @patch("agento.framework.cli.upgrade.subprocess.run")
    @patch("agento.framework.cli.upgrade._upgrade_cli", return_value="0.5.0")
    @patch("agento.framework.cli.upgrade._fetch_latest_pypi_version", return_value="0.5.0")
    @patch("agento.framework.cli.upgrade.get_package_version", return_value="0.4.0")
    @patch("agento.framework.cli.upgrade.find_compose_file")
    @patch("agento.framework.cli.upgrade.find_project_root")
    def test_defaults_to_latest_pypi_version(
        self,
        mock_root,
        mock_compose,
        mock_ver,
        mock_pypi,
        mock_cli,
        mock_run,
        mock_materialize,
        mock_regen,
        tmp_path: Path,
    ):
        from argparse import Namespace

        from agento.framework.cli.upgrade import UpgradeCommand

        mock_root.return_value = tmp_path
        env = self._setup_project(tmp_path)
        mock_compose.return_value = tmp_path / "docker" / "docker-compose.yml"
        mock_run.return_value = type("R", (), {"returncode": 0})()

        cmd = UpgradeCommand()
        cmd.execute(Namespace(version=None, no_build=False, no_restart=False))

        assert "AGENTO_VERSION=0.5.0\n" in env.read_text()
        mock_pypi.assert_called_once()
        mock_cli.assert_called_once_with("0.5.0")

    @patch("agento.framework.cli.upgrade.regenerate_compose")
    @patch("agento.framework.cli.upgrade.materialize_docker_context")
    @patch("agento.framework.cli.upgrade.subprocess.run")
    @patch("agento.framework.cli.upgrade._upgrade_cli", return_value="0.9.0")
    @patch("agento.framework.cli.upgrade.get_package_version", return_value="0.4.0")
    @patch("agento.framework.cli.upgrade.find_compose_file")
    @patch("agento.framework.cli.upgrade.find_project_root")
    def test_uses_explicit_version(
        self,
        mock_root,
        mock_compose,
        mock_ver,
        mock_cli,
        mock_run,
        mock_materialize,
        mock_regen,
        tmp_path: Path,
    ):
        from argparse import Namespace

        from agento.framework.cli.upgrade import UpgradeCommand

        mock_root.return_value = tmp_path
        env = self._setup_project(tmp_path)
        mock_compose.return_value = tmp_path / "docker" / "docker-compose.yml"
        mock_run.return_value = type("R", (), {"returncode": 0})()

        cmd = UpgradeCommand()
        cmd.execute(Namespace(version="0.9.0", no_build=False, no_restart=False))

        assert "AGENTO_VERSION=0.9.0\n" in env.read_text()
        mock_cli.assert_called_once_with("0.9.0")

    @patch("agento.framework.cli.upgrade.regenerate_compose")
    @patch("agento.framework.cli.upgrade.materialize_docker_context")
    @patch("agento.framework.cli.upgrade.subprocess.run")
    @patch("agento.framework.cli.upgrade._upgrade_cli", return_value="0.5.0")
    @patch("agento.framework.cli.upgrade.get_package_version", return_value="0.5.0")
    @patch("agento.framework.cli.upgrade.find_compose_file")
    @patch("agento.framework.cli.upgrade.find_project_root")
    def test_skips_cli_upgrade_when_already_at_version(
        self,
        mock_root,
        mock_compose,
        mock_ver,
        mock_cli,
        mock_run,
        mock_materialize,
        mock_regen,
        tmp_path: Path,
    ):
        from argparse import Namespace

        from agento.framework.cli.upgrade import UpgradeCommand

        mock_root.return_value = tmp_path
        env = self._setup_project(tmp_path)
        mock_compose.return_value = tmp_path / "docker" / "docker-compose.yml"
        mock_run.return_value = type("R", (), {"returncode": 0})()

        cmd = UpgradeCommand()
        cmd.execute(Namespace(version="0.5.0", no_build=False, no_restart=False))

        # CLI upgrade not called because current == target
        mock_cli.assert_not_called()
        assert "AGENTO_VERSION=0.5.0\n" in env.read_text()

    @patch("agento.framework.cli.upgrade.regenerate_compose")
    @patch("agento.framework.cli.upgrade.materialize_docker_context")
    @patch("agento.framework.cli.upgrade.subprocess.run")
    @patch("agento.framework.cli.upgrade._upgrade_cli", return_value="0.6.0")
    @patch("agento.framework.cli.upgrade.get_package_version", return_value="0.5.0")
    @patch("agento.framework.cli.upgrade.find_compose_file")
    @patch("agento.framework.cli.upgrade.find_project_root")
    def test_upgrade_bumps_pin_and_regenerates(
        self,
        mock_root,
        mock_compose,
        mock_ver,
        mock_cli,
        mock_run,
        mock_materialize,
        mock_regen,
        tmp_path: Path,
    ):
        from argparse import Namespace

        from agento.framework.cli.upgrade import UpgradeCommand

        mock_root.return_value = tmp_path
        self._setup_project(tmp_path)
        # Pre-existing project pyproject.toml — must have its pin bumped.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\n'
            'name = "myproj"\n'
            'version = "0.1.0"\n'
            'dependencies = ["agento-core==0.5.0"]\n'
        )
        mock_compose.return_value = tmp_path / "docker" / "docker-compose.yml"
        mock_run.return_value = type("R", (), {"returncode": 0})()

        cmd = UpgradeCommand()
        cmd.execute(Namespace(version="0.6.0", no_build=False, no_restart=False))

        # Project pyproject.toml pin bumped to target version
        pp = (tmp_path / "pyproject.toml").read_text()
        assert "agento-core==0.6.0" in pp
        # Provisioning helpers were invoked
        mock_materialize.assert_called_once()
        mock_regen.assert_called_once()
        # An existing project that predates the store split gets both roots, or
        # the toolbox bind lands on a directory that is missing `store`.
        assert (tmp_path / "storage" / "versioned-artifacts" / "store").is_dir()
        assert (tmp_path / "storage" / "versioned-artifacts" / "published").is_dir()

    @patch("agento.framework.cli.upgrade.regenerate_compose")
    @patch("agento.framework.cli.upgrade.materialize_docker_context")
    @patch("agento.framework.cli.upgrade.subprocess.run")
    @patch("agento.framework.cli.upgrade._upgrade_cli", return_value="0.6.0")
    @patch("agento.framework.cli.upgrade.get_package_version", return_value="0.5.0")
    @patch("agento.framework.cli.upgrade.find_compose_file")
    @patch("agento.framework.cli.upgrade.find_project_root")
    def test_no_build_flag_skips_image_build(
        self,
        mock_root,
        mock_compose,
        mock_ver,
        mock_cli,
        mock_run,
        mock_materialize,
        mock_regen,
        tmp_path: Path,
    ):
        from argparse import Namespace

        from agento.framework.cli.upgrade import UpgradeCommand

        mock_root.return_value = tmp_path
        self._setup_project(tmp_path)
        mock_compose.return_value = tmp_path / "docker" / "docker-compose.yml"
        mock_run.return_value = type("R", (), {"returncode": 0})()

        cmd = UpgradeCommand()
        cmd.execute(Namespace(version="0.6.0", no_build=True, no_restart=False))

        # No `docker build` or `docker compose build` invocations when --no-build set.
        invocations = [tuple(call.args[0]) for call in mock_run.call_args_list]
        assert not any(
            len(inv) >= 4 and inv[0] == "docker" and "build" in inv
            for inv in invocations
        )

    @patch("agento.framework.cli.upgrade.build_base_images")
    @patch("agento.framework.cli.upgrade.regenerate_compose")
    @patch("agento.framework.cli.upgrade.materialize_docker_context")
    @patch("agento.framework.cli.upgrade.subprocess.run")
    @patch("agento.framework.cli.upgrade._upgrade_cli", return_value="0.9.4")
    @patch("agento.framework.cli.upgrade.get_package_version", return_value="0.9.3")
    @patch("agento.framework.cli.upgrade.find_compose_file")
    @patch("agento.framework.cli.upgrade.find_project_root")
    def test_pre_builds_base_images_before_compose_build(
        self,
        mock_root,
        mock_compose,
        mock_ver,
        mock_cli,
        mock_run,
        mock_materialize,
        mock_regen,
        mock_build_base,
        tmp_path: Path,
    ):
        """Regression: without build_base_images, an override that
        FROM agento-toolbox:<version> fails with 'pull access denied' because
        the managed base tag was never built."""
        from argparse import Namespace

        from agento.framework.cli.upgrade import UpgradeCommand

        mock_root.return_value = tmp_path
        self._setup_project(tmp_path)
        mock_compose.return_value = tmp_path / "docker" / "docker-compose.yml"
        mock_run.return_value = type("R", (), {"returncode": 0})()

        cmd = UpgradeCommand()
        cmd.execute(Namespace(version="0.9.4", no_build=False, no_restart=False))

        # build_base_images must be called with the target version BEFORE
        # the first `docker compose build`.
        mock_build_base.assert_called_once_with(tmp_path, "0.9.4")
        # Sanity: compose builds still happen after.
        compose_builds = [
            tuple(call.args[0]) for call in mock_run.call_args_list
            if len(call.args[0]) >= 4 and call.args[0][:2] == ["docker", "compose"]
            and "build" in call.args[0]
        ]
        assert len(compose_builds) >= 1

    @patch("agento.framework.cli.upgrade.build_base_images")
    @patch("agento.framework.cli.upgrade.regenerate_compose")
    @patch("agento.framework.cli.upgrade.materialize_docker_context")
    @patch("agento.framework.cli.upgrade.subprocess.run")
    @patch("agento.framework.cli.upgrade._upgrade_cli", return_value="0.9.4")
    @patch("agento.framework.cli.upgrade.get_package_version", return_value="0.9.3")
    @patch("agento.framework.cli.upgrade.find_compose_file")
    @patch("agento.framework.cli.upgrade.find_project_root")
    def test_no_build_flag_also_skips_base_image_build(
        self,
        mock_root,
        mock_compose,
        mock_ver,
        mock_cli,
        mock_run,
        mock_materialize,
        mock_regen,
        mock_build_base,
        tmp_path: Path,
    ):
        from argparse import Namespace

        from agento.framework.cli.upgrade import UpgradeCommand

        mock_root.return_value = tmp_path
        self._setup_project(tmp_path)
        mock_compose.return_value = tmp_path / "docker" / "docker-compose.yml"
        mock_run.return_value = type("R", (), {"returncode": 0})()

        cmd = UpgradeCommand()
        cmd.execute(Namespace(version="0.9.4", no_build=True, no_restart=False))

        mock_build_base.assert_not_called()


class TestBackfillOrWarnCliPin:
    """Verifies the sticky-pin policy: backfill when missing, warn when stale,
    never overwrite an existing value (customers may have intentionally pinned)."""

    def test_backfills_when_missing(self, tmp_path: Path):
        from agento.framework.cli.upgrade import _backfill_or_warn_cli_pin

        env = tmp_path / ".env"
        env.write_text("AGENTO_VERSION=0.5.0\n")
        existing = {"AGENTO_VERSION": "0.5.0"}

        _backfill_or_warn_cli_pin(
            env, existing,
            key="CLAUDE_CODE_VERSION",
            default="~2.1.142",
            display="claude-code",
        )

        assert "CLAUDE_CODE_VERSION=~2.1.142" in env.read_text()

    def test_warns_when_pin_is_older_than_default(self, tmp_path: Path, capsys):
        from agento.framework.cli.upgrade import _backfill_or_warn_cli_pin

        env = tmp_path / ".env"
        env.write_text("CLAUDE_CODE_VERSION=~2.1.100\n")
        existing = {"CLAUDE_CODE_VERSION": "~2.1.100"}

        _backfill_or_warn_cli_pin(
            env, existing,
            key="CLAUDE_CODE_VERSION",
            default="~2.1.142",
            display="claude-code",
        )

        # Sticky: customer's pin is preserved.
        assert "CLAUDE_CODE_VERSION=~2.1.100" in env.read_text()
        assert "~2.1.142" not in env.read_text()
        # Warning surfaced so customer knows they're behind.
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "~2.1.100" in combined
        assert "~2.1.142" in combined

    def test_silent_when_pin_is_newer_than_default(self, tmp_path: Path, capsys):
        from agento.framework.cli.upgrade import _backfill_or_warn_cli_pin

        env = tmp_path / ".env"
        env.write_text("CLAUDE_CODE_VERSION=~2.1.200\n")
        existing = {"CLAUDE_CODE_VERSION": "~2.1.200"}

        _backfill_or_warn_cli_pin(
            env, existing,
            key="CLAUDE_CODE_VERSION",
            default="~2.1.142",
            display="claude-code",
        )

        # Sticky: customer is ahead, no warning.
        assert "CLAUDE_CODE_VERSION=~2.1.200" in env.read_text()
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "older than" not in combined

    def test_silent_when_pin_matches_default(self, tmp_path: Path, capsys):
        from agento.framework.cli.upgrade import _backfill_or_warn_cli_pin

        env = tmp_path / ".env"
        env.write_text("CLAUDE_CODE_VERSION=~2.1.142\n")
        existing = {"CLAUDE_CODE_VERSION": "~2.1.142"}

        _backfill_or_warn_cli_pin(
            env, existing,
            key="CLAUDE_CODE_VERSION",
            default="~2.1.142",
            display="claude-code",
        )

        out = capsys.readouterr()
        assert "older than" not in (out.out + out.err)

    def test_unparseable_pin_does_not_crash(self, tmp_path: Path):
        from agento.framework.cli.upgrade import _backfill_or_warn_cli_pin

        env = tmp_path / ".env"
        env.write_text("CLAUDE_CODE_VERSION=latest\n")
        existing = {"CLAUDE_CODE_VERSION": "latest"}

        # Comparison skipped silently; pin preserved.
        _backfill_or_warn_cli_pin(
            env, existing,
            key="CLAUDE_CODE_VERSION",
            default="~2.1.142",
            display="claude-code",
        )

        assert "CLAUDE_CODE_VERSION=latest" in env.read_text()
