from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.agent_manager.auth import (
    AuthenticationError,
    AuthResult,
    authenticate_interactive,
    save_credentials,
)
from agento.framework.harness import clear
from tests.harness_fixtures import register_builtin_harnesses


@pytest.fixture(autouse=True)
def _register_harnesses():
    """Populate the harness registry — authenticators are keyed by credential scope."""
    register_builtin_harnesses()
    yield
    clear()


class TestAuthenticateInteractiveClaude:
    """Tests for authenticate_interactive with "claude"."""

    @patch("agento.modules.claude.src.auth.Path.home")
    @patch("agento.modules.claude.src.auth._run_cli")
    def test_extracts_credentials_from_claude(self, mock_run_cli, mock_home, tmp_path):
        """Successful Claude auth extracts accessToken from .credentials.json."""
        mock_home.return_value = tmp_path

        def setup_credentials(cmd, home, name):
            claude_dir = Path(home) / ".claude"
            claude_dir.mkdir(parents=True)
            creds = {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat01-test-access",
                    "refreshToken": "sk-ant-ort01-test-refresh",
                    "expiresAt": 1800000000000,
                    "subscriptionType": "team",
                }
            }
            (claude_dir / ".credentials.json").write_text(json.dumps(creds))

        mock_run_cli.side_effect = setup_credentials

        with patch("agento.framework.agent_manager.auth.tempfile.mkdtemp", return_value=str(tmp_path)):
            result = authenticate_interactive("claude")

        assert result.subscription_key == "sk-ant-oat01-test-access"
        assert result.refresh_token == "sk-ant-ort01-test-refresh"
        assert result.expires_at == 1800000000000
        assert result.subscription_type == "team"

    @patch("agento.modules.claude.src.auth.Path.home")
    @patch("agento.modules.claude.src.auth._run_cli")
    def test_raises_when_credentials_file_missing(self, mock_run_cli, mock_home, tmp_path):
        """Raises AuthenticationError when .credentials.json is not created."""
        mock_home.return_value = tmp_path
        mock_run_cli.return_value = None  # CLI ran but didn't create file

        with patch("agento.framework.agent_manager.auth.tempfile.mkdtemp", return_value=str(tmp_path)), \
             pytest.raises(AuthenticationError, match="credentials file not found"):
            authenticate_interactive("claude")

    @patch("agento.modules.claude.src.auth.Path.home")
    @patch("agento.modules.claude.src.auth._run_cli")
    def test_raises_when_no_access_token(self, mock_run_cli, mock_home, tmp_path):
        """Raises AuthenticationError when accessToken is missing."""
        mock_home.return_value = tmp_path

        def setup_empty_creds(cmd, home, name):
            claude_dir = Path(home) / ".claude"
            claude_dir.mkdir(parents=True)
            (claude_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {}}))

        mock_run_cli.side_effect = setup_empty_creds

        with patch("agento.framework.agent_manager.auth.tempfile.mkdtemp", return_value=str(tmp_path)), \
             pytest.raises(AuthenticationError, match="no accessToken"):
            authenticate_interactive("claude")

    @patch("agento.modules.claude.src.auth.Path.home")
    @patch("agento.framework.agent_manager.auth.shutil.rmtree")
    @patch("agento.modules.claude.src.auth._run_cli")
    def test_cleans_up_temp_dir_on_success(self, mock_run_cli, mock_rmtree, mock_home, tmp_path):
        """Temp directory is cleaned up after successful auth."""
        mock_home.return_value = tmp_path

        def setup_credentials(cmd, home, name):
            claude_dir = Path(home) / ".claude"
            claude_dir.mkdir(parents=True)
            creds = {"claudeAiOauth": {"accessToken": "sk-test"}}
            (claude_dir / ".credentials.json").write_text(json.dumps(creds))

        mock_run_cli.side_effect = setup_credentials

        with patch("agento.framework.agent_manager.auth.tempfile.mkdtemp", return_value=str(tmp_path)):
            authenticate_interactive("claude")

        mock_rmtree.assert_called_once_with(str(tmp_path), ignore_errors=True)

    @patch("agento.framework.agent_manager.auth.shutil.rmtree")
    @patch("agento.modules.claude.src.auth._run_cli")
    def test_cleans_up_temp_dir_on_failure(self, mock_run_cli, mock_rmtree, tmp_path):
        """Temp directory is cleaned up even when auth fails."""
        mock_run_cli.side_effect = AuthenticationError("CLI failed")

        with patch("agento.framework.agent_manager.auth.tempfile.mkdtemp", return_value=str(tmp_path)), \
             pytest.raises(AuthenticationError):
            authenticate_interactive("claude")

        mock_rmtree.assert_called_once_with(str(tmp_path), ignore_errors=True)


class TestAuthenticateInteractiveCodex:
    """Tests for authenticate_interactive with "codex"."""

    @patch("agento.modules.codex.src.auth._run_cli")
    def test_extracts_credentials_from_codex(self, mock_run_cli, tmp_path):
        """Successful Codex auth extracts access_token from auth.json."""
        def setup_credentials(cmd, home, name):
            codex_dir = Path(home) / ".codex"
            codex_dir.mkdir(parents=True)
            creds = {
                "tokens": {
                    "access_token": "sk-openai-test-access",
                    "refresh_token": "sk-openai-test-refresh",
                }
            }
            (codex_dir / "auth.json").write_text(json.dumps(creds))

        mock_run_cli.side_effect = setup_credentials

        with patch("agento.framework.agent_manager.auth.tempfile.mkdtemp", return_value=str(tmp_path)):
            result = authenticate_interactive("codex")

        assert result.subscription_key == "sk-openai-test-access"
        assert result.refresh_token == "sk-openai-test-refresh"
        assert result.expires_at is None
        assert result.subscription_type is None

    @patch("agento.modules.codex.src.auth._run_cli")
    def test_raises_when_auth_json_missing(self, mock_run_cli, tmp_path):
        """Raises AuthenticationError when auth.json is not created."""
        mock_run_cli.return_value = None

        with patch("agento.framework.agent_manager.auth.tempfile.mkdtemp", return_value=str(tmp_path)), \
             pytest.raises(AuthenticationError, match=r"auth\.json not found"):
            authenticate_interactive("codex")

    @patch("agento.modules.codex.src.auth._run_cli")
    def test_raises_when_no_access_token(self, mock_run_cli, tmp_path):
        """Raises AuthenticationError when access_token is missing."""
        def setup_empty_creds(cmd, home, name):
            codex_dir = Path(home) / ".codex"
            codex_dir.mkdir(parents=True)
            (codex_dir / "auth.json").write_text(json.dumps({"tokens": {}}))

        mock_run_cli.side_effect = setup_empty_creds

        with patch("agento.framework.agent_manager.auth.tempfile.mkdtemp", return_value=str(tmp_path)), \
             pytest.raises(AuthenticationError, match="no access_token"):
            authenticate_interactive("codex")


class TestAuthenticatorLookup:
    """Authenticators come from the harness registry, keyed by credential scope."""

    def test_unknown_scope_raises(self):
        clear()
        with pytest.raises(ValueError, match="No authenticator registered"):
            authenticate_interactive("claude")


class TestRunCli:
    """Tests for _run_cli helper."""

    @patch("agento.framework.agent_manager.auth.subprocess.run")
    def test_runs_with_isolated_home(self, mock_subprocess):
        """CLI command is executed with HOME set to the temp directory."""
        mock_subprocess.return_value = MagicMock(returncode=0)
        from agento.framework.agent_manager.auth import _run_cli

        _run_cli(["claude", "auth", "login"], "/tmp/test_home", "Claude")

        mock_subprocess.assert_called_once()
        call_env = mock_subprocess.call_args.kwargs["env"]
        assert call_env["HOME"] == "/tmp/test_home"

    @patch("agento.framework.agent_manager.auth.subprocess.run", side_effect=FileNotFoundError)
    def test_raises_when_cli_not_found(self, mock_subprocess):
        """Raises AuthenticationError when CLI binary is not found."""
        from agento.framework.agent_manager.auth import _run_cli

        with pytest.raises(AuthenticationError, match="CLI not found"):
            _run_cli(["claude", "auth", "login"], "/tmp/test_home", "Claude")

    @patch("agento.framework.agent_manager.auth.subprocess.run")
    def test_raises_on_nonzero_exit(self, mock_subprocess):
        """Raises AuthenticationError when CLI returns non-zero."""
        mock_subprocess.return_value = MagicMock(returncode=1)
        from agento.framework.agent_manager.auth import _run_cli

        with pytest.raises(AuthenticationError, match="exit code 1"):
            _run_cli(["claude", "auth", "login"], "/tmp/test_home", "Claude")


class TestSaveCredentials:
    def test_saves_to_json_file(self, tmp_path):
        """Saves AuthResult fields to a JSON file."""
        output = tmp_path / "creds.json"
        auth = AuthResult(
            subscription_key="sk-test",
            refresh_token="sk-refresh",
            expires_at=1800000000000,
            subscription_type="team",
        )

        save_credentials(auth, str(output))

        data = json.loads(output.read_text())
        assert data["subscription_key"] == "sk-test"
        assert data["refresh_token"] == "sk-refresh"
        assert data["expires_at"] == 1800000000000
        assert data["subscription_type"] == "team"

    def test_creates_parent_directories(self, tmp_path):
        """Creates parent directories if they don't exist."""
        output = tmp_path / "nested" / "dir" / "creds.json"

        save_credentials(
            AuthResult(subscription_key="sk-test", refresh_token=None, expires_at=None, subscription_type=None),
            str(output),
        )

        assert output.is_file()

    def test_handles_none_values(self, tmp_path):
        """None values are preserved as null in JSON."""
        output = tmp_path / "creds.json"
        auth = AuthResult(
            subscription_key="sk-test",
            refresh_token=None,
            expires_at=None,
            subscription_type=None,
        )

        save_credentials(auth, str(output))

        data = json.loads(output.read_text())
        assert data["refresh_token"] is None
        assert data["expires_at"] is None
        assert data["subscription_type"] is None
