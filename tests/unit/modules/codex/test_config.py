"""Tests for CodexWorkspaceAdapter — .codex/config.toml with MCP servers."""
from __future__ import annotations

import tomllib
from datetime import datetime

import pytest

from agento.framework.agent_manager.models import CredentialRecord, CredentialStatus
from agento.modules.codex.src.config import CodexWorkspaceAdapter

_EPOCH = datetime(2000, 1, 1)


def _make_token(credentials: dict) -> CredentialRecord:
    return CredentialRecord(
        id=1,
        scope="codex",
        type="oauth",
        label="test",
        credentials=credentials,
        token_limit=0,
        enabled=True,
        status=CredentialStatus.OK,
        priority=0,
        error_msg=None,
        expires_at=None,
        used_at=None,
        created_at=_EPOCH,
        updated_at=_EPOCH,
    )


@pytest.fixture
def writer():
    return CodexWorkspaceAdapter()


@pytest.fixture
def work_dir(tmp_path):
    d = tmp_path / "workspace" / "main" / "dev"
    d.mkdir(parents=True)
    return d


class TestPrepareWorkspace:
    TOOLBOX = "http://toolbox:3001"

    def test_writes_model(self, writer, work_dir):
        writer.prepare_workspace(work_dir, {"model": "o3"}, toolbox_url=self.TOOLBOX)
        content = (work_dir / ".codex" / "config.toml").read_text()
        assert 'model = "o3"' in content

    def test_writes_approval_mode(self, writer, work_dir):
        writer.prepare_workspace(
            work_dir, {"model": "o3", "codex/approval_mode": "full-auto"},
            toolbox_url=self.TOOLBOX,
        )
        content = (work_dir / ".codex" / "config.toml").read_text()
        assert 'approval_mode = "full-auto"' in content

    def test_user_can_shadow_toolbox_entry(self, writer, work_dir):
        servers = '{"toolbox": {"url": "http://toolbox:3001/sse"}}'
        writer.prepare_workspace(
            work_dir, {"model": "o3", "mcp/servers": servers},
            agent_view_id=2, toolbox_url=self.TOOLBOX,
        )
        data = tomllib.loads((work_dir / ".codex" / "config.toml").read_text())
        assert data["mcp_servers"]["toolbox"]["type"] == "sse"
        assert "agent_view_id=2" in data["mcp_servers"]["toolbox"]["url"]

    def test_auto_injects_toolbox_streamable_http(self, writer, work_dir):
        writer.prepare_workspace(
            work_dir, {"model": "o3"},
            agent_view_id=3, toolbox_url=self.TOOLBOX,
        )
        data = tomllib.loads((work_dir / ".codex" / "config.toml").read_text())
        assert data["mcp_servers"]["toolbox"]["type"] == "streamable_http"
        assert data["mcp_servers"]["toolbox"]["url"].startswith("http://toolbox:3001/mcp")
        assert "agent_view_id=3" in data["mcp_servers"]["toolbox"]["url"]

    def test_no_agent_view_id_leaves_url_unchanged(self, writer, work_dir):
        writer.prepare_workspace(work_dir, {"model": "o3"}, toolbox_url=self.TOOLBOX)
        data = tomllib.loads((work_dir / ".codex" / "config.toml").read_text())
        assert data["mcp_servers"]["toolbox"]["url"] == "http://toolbox:3001/mcp"

    def test_empty_config_still_writes_toolbox_entry(self, writer, work_dir):
        writer.prepare_workspace(work_dir, {}, toolbox_url=self.TOOLBOX)
        data = tomllib.loads((work_dir / ".codex" / "config.toml").read_text())
        assert data["mcp_servers"]["toolbox"]["type"] == "streamable_http"

    def test_ignores_invalid_extras_json(self, writer, work_dir):
        writer.prepare_workspace(
            work_dir, {"model": "o3", "mcp/servers": "not-json{"},
            toolbox_url=self.TOOLBOX,
        )
        data = tomllib.loads((work_dir / ".codex" / "config.toml").read_text())
        # Toolbox is still auto-injected; bad extras are ignored.
        assert list(data["mcp_servers"].keys()) == ["toolbox"]

    def test_extras_merge_with_toolbox(self, writer, work_dir):
        import json
        extras = json.dumps({
            "other": {"url": "http://other:4000/mcp"},
        })
        writer.prepare_workspace(
            work_dir, {"mcp/servers": extras},
            agent_view_id=1, toolbox_url=self.TOOLBOX,
        )
        data = tomllib.loads((work_dir / ".codex" / "config.toml").read_text())
        assert data["mcp_servers"]["toolbox"]["type"] == "streamable_http"
        assert data["mcp_servers"]["other"]["type"] == "streamable_http"


class TestInjectRuntimeParams:
    def test_appends_params_to_toml_urls(self, writer, work_dir):
        codex_dir = work_dir / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text(
            'model = "o3"\n'
            "\n[mcp_servers.toolbox]\n"
            'type = "sse"\n'
            'url = "http://toolbox:3001/sse?agent_view_id=2"\n'
        )

        writer.inject_runtime_params(work_dir, job_id=10)

        data = tomllib.loads((codex_dir / "config.toml").read_text())
        assert data["mcp_servers"]["toolbox"]["url"] == (
            "http://toolbox:3001/sse?agent_view_id=2&job_id=10"
        )

    def test_noop_when_no_config_toml(self, writer, work_dir):
        writer.inject_runtime_params(work_dir, job_id=10)
        assert not (work_dir / ".codex" / "config.toml").exists()

    def test_preserves_model_and_approval_mode(self, writer, work_dir):
        codex_dir = work_dir / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text(
            'model = "gpt-5"\n'
            'approval_mode = "full-auto"\n'
            "\n[mcp_servers.toolbox]\n"
            'type = "sse"\n'
            'url = "http://toolbox:3001/sse?agent_view_id=1"\n'
        )

        writer.inject_runtime_params(work_dir, job_id=5)

        data = tomllib.loads((codex_dir / "config.toml").read_text())
        assert data["model"] == "gpt-5"
        assert data["approval_mode"] == "full-auto"
        assert "job_id=5" in data["mcp_servers"]["toolbox"]["url"]


class TestWriteCredentials:
    def _make_token(self, type_: str, credentials: dict, **kwargs):
        return CredentialRecord(
            id=kwargs.get("id", 1),
            scope="codex",
            type=type_,
            label=kwargs.get("label", "test"),
            credentials=credentials,
            token_limit=0,
            enabled=True,
            status=CredentialStatus.OK,
            priority=0,
            error_msg=None,
            expires_at=None,
            used_at=None,
            created_at=_EPOCH,
            updated_at=_EPOCH,
        )

    def test_oauth_writes_raw_auth_json(self, writer, work_dir):
        raw = {"tokens": {"access_token": "x", "refresh_token": "y"}}
        token = self._make_token("oauth", {
            "raw_auth": raw, "subscription_key": "x", "refresh_token": "y",
        })
        writer.write_credentials(work_dir, token)
        import json as _json
        assert _json.loads((work_dir / ".codex" / "auth.json").read_text()) == raw

    def test_oauth_sets_chmod_600(self, writer, work_dir):
        raw = {"tokens": {"access_token": "codex-access", "refresh_token": "codex-refresh"}}
        creds = {
            "subscription_key": "codex-access",
            "refresh_token": "codex-refresh",
            "expires_at": None,
            "subscription_type": None,
            "id_token": "idtok",
            "raw_auth": raw,
        }
        writer.write_credentials(work_dir, self._make_token("oauth", creds))

        path = work_dir / ".codex" / "auth.json"
        assert path.is_file()
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_oauth_skips_without_raw_auth(self, writer, work_dir, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="agento.modules.codex.src.config"):
            writer.write_credentials(work_dir, self._make_token("oauth", {"subscription_key": "codex-access"}))
        assert not (work_dir / ".codex" / "auth.json").exists()
        assert "raw_auth" in caplog.text

    def test_openai_api_key_invokes_codex_login_with_stdin(self, writer, work_dir, monkeypatch):
        import subprocess
        from unittest.mock import MagicMock
        fake_run = MagicMock(return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""))
        monkeypatch.setattr("agento.modules.codex.src.config.subprocess.run", fake_run)

        token = self._make_token("openai_api_key", {"api_key": "sk-X"})
        writer.write_credentials(work_dir, token)

        fake_run.assert_called_once()
        call_args = fake_run.call_args
        assert call_args[0][0] == ["codex", "login", "--with-api-key"]
        assert call_args.kwargs.get("input") == "sk-X"
        env = call_args.kwargs.get("env") or {}
        assert env.get("HOME") == str(work_dir)
        assert "sk-X" not in " ".join(call_args[0][0])

    def test_openai_api_key_raises_when_missing(self, writer, work_dir):
        from agento.framework.agent_manager.errors import AuthenticationError
        token = self._make_token("openai_api_key", {})
        with pytest.raises(AuthenticationError, match="openai_api_key"):
            writer.write_credentials(work_dir, token)

    def test_codex_access_token_invokes_codex_login_with_stdin(self, writer, work_dir, monkeypatch):
        import subprocess
        from unittest.mock import MagicMock
        fake_run = MagicMock(return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""))
        monkeypatch.setattr("agento.modules.codex.src.config.subprocess.run", fake_run)

        token = self._make_token("codex_access_token", {
            "access_token": "eyJ.payload.sig", "expires_at": 9999999999})
        writer.write_credentials(work_dir, token)

        fake_run.assert_called_once()
        call_args = fake_run.call_args
        assert call_args[0][0] == ["codex", "login", "--with-access-token"]
        assert call_args.kwargs.get("input") == "eyJ.payload.sig"
        env = call_args.kwargs.get("env") or {}
        assert env.get("HOME") == str(work_dir)
        assert "eyJ.payload.sig" not in " ".join(call_args[0][0])

    def test_codex_access_token_raises_on_nonzero_exit(self, writer, work_dir, monkeypatch, caplog):
        import logging
        import subprocess
        from unittest.mock import MagicMock

        from agento.framework.agent_manager.errors import AuthenticationError

        fake_run = MagicMock(return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="invalid agent identity JWT format"))
        monkeypatch.setattr("agento.modules.codex.src.config.subprocess.run", fake_run)

        token = self._make_token("codex_access_token", {
            "access_token": "eyJ.bad.sig", "expires_at": 9999999999})
        with (
            caplog.at_level(logging.WARNING, logger="agento.modules.codex.src.config"),
            pytest.raises(AuthenticationError),
        ):
            writer.write_credentials(work_dir, token)
        assert "eyJ.bad.sig" not in caplog.text


class TestOwnedPaths:
    def test_returns_codex_dir(self, writer):
        files, dirs = writer.owned_paths()
        assert files == set()
        assert dirs == {".codex"}


class TestMigrateLegacyWorkspaceConfig:
    def test_migrates_toolbox_mcp_from_legacy_workspace_codex_config(self, writer, work_dir, tmp_path):
        workspace_root = tmp_path / "workspace"
        legacy_codex = workspace_root / ".codex"
        legacy_codex.mkdir(parents=True)
        (legacy_codex / "config.toml").write_text(
            'model = "gpt-5.4"\n'
            "\n[mcp_servers.toolbox]\n"
            'type = "streamable_http"\n'
            'url = "http://toolbox:3001/mcp?agent_view_id=2"\n'
        )

        writer.migrate_legacy_workspace_config(work_dir, workspace_root)

        data = tomllib.loads((work_dir / ".codex" / "config.toml").read_text())
        assert data["mcp_servers"]["toolbox"]["url"] == "http://toolbox:3001/mcp?agent_view_id=2"
        assert data["model"] == "gpt-5.4"

    def test_merges_legacy_mcp_with_existing_build_config(self, writer, work_dir, tmp_path):
        codex_dir = work_dir / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text(
            'model = "gpt-5.4"\n'
            "\n[mcp_servers.other]\n"
            'type = "sse"\n'
            'url = "http://other:4000/sse"\n'
        )

        workspace_root = tmp_path / "workspace"
        legacy_codex = workspace_root / ".codex"
        legacy_codex.mkdir(parents=True)
        (legacy_codex / "config.toml").write_text(
            "\n[mcp_servers.toolbox]\n"
            'type = "streamable_http"\n'
            'url = "http://toolbox:3001/mcp?agent_view_id=2"\n'
        )

        writer.migrate_legacy_workspace_config(work_dir, workspace_root)

        data = tomllib.loads((work_dir / ".codex" / "config.toml").read_text())
        assert data["model"] == "gpt-5.4"
        assert data["mcp_servers"]["other"]["url"] == "http://other:4000/sse"
        assert data["mcp_servers"]["toolbox"]["url"] == "http://toolbox:3001/mcp?agent_view_id=2"


class TestCaptureRefreshedCredentials:
    def _make_token(self, refresh_token="tok-A", access_token="acc-A"):
        from unittest.mock import MagicMock

        token = MagicMock()
        token.credentials = {
            "raw_auth": {"tokens": {"refresh_token": refresh_token, "access_token": access_token}},
            "refresh_token": refresh_token,
            "subscription_key": access_token,
        }
        token.type = "oauth"
        token.scope = "codex"
        token.label = "my-codex"
        token.token_limit = 0
        return token

    def _make_typed_token(self, type_: str, credentials: dict):
        return CredentialRecord(
            id=1,
            scope="codex",
            type=type_,
            label="test",
            credentials=credentials,
            token_limit=0,
            enabled=True,
            status=CredentialStatus.OK,
            priority=0,
            error_msg=None,
            expires_at=None,
            used_at=None,
            created_at=_EPOCH,
            updated_at=_EPOCH,
        )

    def test_noop_when_no_auth_json(self, writer, work_dir):
        from unittest.mock import MagicMock, patch
        token = self._make_token()
        mock_conn = MagicMock()
        with patch("agento.modules.codex.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, mock_conn)
        mock_reg.assert_not_called()

    def test_noop_when_refresh_token_unchanged(self, writer, work_dir):
        import json
        from unittest.mock import MagicMock, patch
        codex_dir = work_dir / ".codex"
        codex_dir.mkdir(parents=True)
        raw = {"tokens": {"refresh_token": "tok-A", "access_token": "acc-A"}}
        (codex_dir / "auth.json").write_text(json.dumps(raw))

        token = self._make_token(refresh_token="tok-A")
        with patch("agento.modules.codex.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, MagicMock())
        mock_reg.assert_not_called()

    def test_upserts_db_when_refresh_token_changed(self, writer, work_dir):
        import json
        from unittest.mock import MagicMock, patch

        codex_dir = work_dir / ".codex"
        codex_dir.mkdir(parents=True)
        refreshed = {"tokens": {"refresh_token": "tok-B", "access_token": "acc-B"}}
        (codex_dir / "auth.json").write_text(json.dumps(refreshed))

        token = self._make_token(refresh_token="tok-A")
        mock_conn = MagicMock()

        with patch("agento.modules.codex.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, mock_conn)

        mock_reg.assert_called_once()
        call_args = mock_reg.call_args
        # Signature: update_refreshed_credentials(conn, token_id, new_creds, logger=...)
        assert call_args[0][0] is mock_conn
        assert call_args[0][1] == token.id   # targeted by id (no agent_type/label/token_limit)
        saved_creds = call_args[0][2]
        assert saved_creds["raw_auth"] == refreshed
        assert saved_creds["refresh_token"] == "tok-B"
        assert saved_creds["subscription_key"] == "acc-B"

    def test_noop_for_codex_access_token_type(self, writer, work_dir):
        import json
        from unittest.mock import MagicMock, patch
        codex_dir = work_dir / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "auth.json").write_text(json.dumps({
            "tokens": {"refresh_token": "rotated", "access_token": "rotated-acc"}}))

        token = self._make_typed_token("codex_access_token", {
            "access_token": "eyJ.x.sig", "expires_at": 9999999999})
        with patch("agento.modules.codex.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, MagicMock())
        mock_reg.assert_not_called()

    def test_noop_for_openai_api_key_type(self, writer, work_dir):
        """API-key rows have no rotated auth.json; capture must skip."""
        from unittest.mock import MagicMock, patch
        token = self._make_typed_token("openai_api_key", {"api_key": "sk-X"})
        with patch("agento.modules.codex.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, MagicMock())
        mock_reg.assert_not_called()



class TestCredentialEnv:
    """Provider-specific credential→env mapping (used by TokenRunner._build_env
    and by the host-side `agento run` env injection path).
    """

    def _typed_token(self, type_: str, credentials: dict) -> CredentialRecord:
        return CredentialRecord(
            id=1, scope="codex", type=type_, label="test",
            credentials=credentials, token_limit=0, enabled=True,
            status=CredentialStatus.OK, priority=0, error_msg=None,
            expires_at=None, used_at=None,
            created_at=_EPOCH, updated_at=_EPOCH,
        )

    def test_oauth_returns_empty(self, writer):
        token = self._typed_token("oauth", {"subscription_key": "acc-x", "refresh_token": "rt"})
        assert writer.credential_env(token) == {}

    def test_codex_access_token_returns_empty(self, writer):
        token = self._typed_token("codex_access_token", {"access_token": "eyJ.x.y"})
        assert writer.credential_env(token) == {}

    def test_openai_api_key_returns_empty_env(self, writer):
        token = self._typed_token("openai_api_key", {"api_key": "sk-X"})
        assert writer.credential_env(token) == {}

    def test_openai_api_key_missing_raises(self, writer):
        token = self._typed_token("openai_api_key", {})
        with pytest.raises(ValueError, match="openai_api_key"):
            writer.credential_env(token)
