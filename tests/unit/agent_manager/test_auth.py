from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agento.framework import workspace_paths
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


class TestAuthenticateInteractive:
    """The framework side; the login itself is ``attended_login`` (claude web-login tests)."""

    def test_home_is_on_the_shared_workspace_and_removed(self, tmp_path, monkeypatch):
        """The runner sees the HOME at the same path; it is removed even on failure."""
        monkeypatch.setattr(workspace_paths, "BASE_WORKSPACE_DIR", str(tmp_path))
        homes = []

        def login(home, _logger):
            homes.append(Path(home))
            raise AuthenticationError("CLI failed")

        with patch("agento.modules.codex.src.auth.CodexCredentialAuthenticator.authenticate_interactive",
                   side_effect=login), pytest.raises(AuthenticationError):
            authenticate_interactive("codex")

        assert homes[0].parent == tmp_path / ".tmp"
        assert not homes[0].exists()


class TestAuthenticatorLookup:
    """Authenticators come from the harness registry, keyed by credential scope."""

    def test_unknown_scope_raises(self):
        clear()
        with pytest.raises(ValueError, match="No authenticator registered"):
            authenticate_interactive("claude")


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
