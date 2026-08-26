"""Tests for ClaudeCredentialAuthenticator — captures full ``claudeAiOauth`` + ``.claude.json``."""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from agento.framework.agent_manager.auth import AuthenticationError
from agento.modules.claude.src.auth import ClaudeCredentialAuthenticator


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "agento.modules.claude.src.auth.Path.home",
        classmethod(lambda _cls: tmp_path),
    )
    return tmp_path


class TestClaudeCredentialAuthenticator:
    def _stub_run_cli(self, home):
        # Simulate a successful interactive login writing files to HOME
        creds = {
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat01-abc",
                "refreshToken": "sk-ant-ort01-def",
                "expiresAt": 1776946615316,
                "scopes": [
                    "user:file_upload",
                    "user:inference",
                    "user:mcp_servers",
                    "user:profile",
                    "user:sessions:claude_code",
                ],
                "subscriptionType": "team",
                "rateLimitTier": "default_claude_max_5x",
            }
        }
        (home / ".claude" / ".credentials.json").write_text(json.dumps(creds))
        (home / ".claude.json").write_text(json.dumps({
            "numStartups": 3,
            "userID": "abc123",
            "oauthAccount": {
                "emailAddress": "m@k.com",
                "organizationName": "My company",
            },
        }))

    def test_captures_full_oauth_payload(self, fake_home):
        strategy = ClaudeCredentialAuthenticator()
        with patch(
            "agento.modules.claude.src.auth._run_cli",
            side_effect=lambda *a, **kw: self._stub_run_cli(fake_home),
        ):
            result = strategy.authenticate_interactive("/ignored/tmp", logging.getLogger("test"))

        assert result.subscription_key == "sk-ant-oat01-abc"
        assert result.refresh_token == "sk-ant-ort01-def"
        assert result.expires_at == 1776946615316
        assert result.subscription_type == "team"

        # raw_auth.credentials preserves the full Claude payload verbatim
        assert result.raw_auth is not None
        raw_creds = result.raw_auth["credentials"]
        assert raw_creds["claudeAiOauth"]["scopes"] == [
            "user:file_upload",
            "user:inference",
            "user:mcp_servers",
            "user:profile",
            "user:sessions:claude_code",
        ]
        assert raw_creds["claudeAiOauth"]["rateLimitTier"] == "default_claude_max_5x"

    def test_captures_claude_json_user_state(self, fake_home):
        strategy = ClaudeCredentialAuthenticator()
        with patch(
            "agento.modules.claude.src.auth._run_cli",
            side_effect=lambda *a, **kw: self._stub_run_cli(fake_home),
        ):
            result = strategy.authenticate_interactive("/ignored/tmp", logging.getLogger("test"))

        assert result.raw_auth is not None
        claude_json = result.raw_auth["claude_json"]
        assert claude_json["oauthAccount"]["emailAddress"] == "m@k.com"
        assert claude_json["oauthAccount"]["organizationName"] == "My company"
        assert claude_json["userID"] == "abc123"

    def test_missing_claude_json_is_ok(self, fake_home):
        # Only .credentials.json is written; .claude.json absent.
        def _only_creds(*_args, **_kw):
            (fake_home / ".claude" / ".credentials.json").write_text(json.dumps({
                "claudeAiOauth": {"accessToken": "sk-x"}
            }))

        strategy = ClaudeCredentialAuthenticator()
        with patch("agento.modules.claude.src.auth._run_cli", side_effect=_only_creds):
            result = strategy.authenticate_interactive("/ignored/tmp", logging.getLogger("test"))

        assert result.subscription_key == "sk-x"
        assert result.raw_auth is not None
        assert result.raw_auth["claude_json"] == {}

    def test_missing_credentials_file_raises(self, fake_home):
        strategy = ClaudeCredentialAuthenticator()
        # _run_cli returns without writing anything
        with (
            patch("agento.modules.claude.src.auth._run_cli", lambda *a, **kw: None),
            pytest.raises(AuthenticationError, match="credentials file not found"),
        ):
            strategy.authenticate_interactive("/ignored/tmp", logging.getLogger("test"))

    def test_credentials_without_access_token_raises(self, fake_home):
        def _no_token(*_args, **_kw):
            (fake_home / ".claude" / ".credentials.json").write_text(
                json.dumps({"claudeAiOauth": {}})
            )

        strategy = ClaudeCredentialAuthenticator()
        with (
            patch("agento.modules.claude.src.auth._run_cli", side_effect=_no_token),
            pytest.raises(AuthenticationError, match="no accessToken"),
        ):
            strategy.authenticate_interactive("/ignored/tmp", logging.getLogger("test"))

    def test_malformed_claude_json_is_tolerated(self, fake_home):
        def _bad_claude_json(*_args, **_kw):
            (fake_home / ".claude" / ".credentials.json").write_text(json.dumps({
                "claudeAiOauth": {"accessToken": "sk-x"}
            }))
            (fake_home / ".claude.json").write_text("not-json{")

        strategy = ClaudeCredentialAuthenticator()
        with patch("agento.modules.claude.src.auth._run_cli", side_effect=_bad_claude_json):
            result = strategy.authenticate_interactive("/ignored/tmp", logging.getLogger("test"))

        assert result.subscription_key == "sk-x"
        assert result.raw_auth is not None
        assert result.raw_auth["claude_json"] == {}


class TestRegisterFromApiKey:
    def test_accepts_sk_ant_prefix(self):
        creds, token_type = ClaudeCredentialAuthenticator().register_from_api_key("sk-ant-abc123")
        assert creds == {"api_key": "sk-ant-abc123"}
        assert token_type == "anthropic_api_key"

    def test_rejects_empty(self):
        with pytest.raises(AuthenticationError):
            ClaudeCredentialAuthenticator().register_from_api_key("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(AuthenticationError):
            ClaudeCredentialAuthenticator().register_from_api_key("   ")

    def test_rejects_openai_proj_prefix(self):
        """OpenAI keys (sk-proj-...) must not be accepted as Anthropic keys."""
        with pytest.raises(AuthenticationError, match="Anthropic"):
            ClaudeCredentialAuthenticator().register_from_api_key("sk-proj-XXXX")

    def test_rejects_openai_svcacct_prefix(self):
        """OpenAI service-account keys (sk-svcacct-...) must not be accepted."""
        with pytest.raises(AuthenticationError, match="Anthropic"):
            ClaudeCredentialAuthenticator().register_from_api_key("sk-svcacct-XXXX")

    def test_strips_surrounding_whitespace(self):
        creds, token_type = ClaudeCredentialAuthenticator().register_from_api_key("  sk-ant-abc  ")
        assert creds == {"api_key": "sk-ant-abc"}
        assert token_type == "anthropic_api_key"


class TestAccountLabel:
    """AG-45: the real OAuth account behind the label, extracted from the captured
    ``.claude.json`` ``oauthAccount.emailAddress``."""

    def test_extracts_email_from_captured_claude_json(self):
        creds = {
            "subscription_key": "sk-ant-oat01-abc",
            "raw_auth": {"claude_json": {"oauthAccount": {"emailAddress": "ops@example.com"}}},
        }
        assert ClaudeCredentialAuthenticator().account_label(creds) == "ops@example.com"

    def test_none_for_api_key_credentials(self):
        assert ClaudeCredentialAuthenticator().account_label({"api_key": "sk-ant-abc"}) is None

    def test_none_when_oauth_account_missing(self):
        creds = {"raw_auth": {"claude_json": {"userID": "u1"}}}
        assert ClaudeCredentialAuthenticator().account_label(creds) is None

    def test_none_when_email_blank(self):
        creds = {"raw_auth": {"claude_json": {"oauthAccount": {"emailAddress": "  "}}}}
        assert ClaudeCredentialAuthenticator().account_label(creds) is None

    def test_none_when_raw_auth_malformed(self):
        assert ClaudeCredentialAuthenticator().account_label({"raw_auth": "nope"}) is None
