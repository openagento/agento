"""Tests for CodexCredentialAuthenticator register-from-X paths."""
from __future__ import annotations

import base64
import json
import logging
import time

import pytest

from agento.framework.agent_manager.errors import AuthenticationError
from agento.modules.codex.src.auth import CodexCredentialAuthenticator


def _make_jwt(payload: dict, header: dict | None = None) -> str:
    header = header or {"alg": "RS256", "typ": "JWT"}

    def _b64(d: dict) -> str:
        raw = json.dumps(d, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{_b64(header)}.{_b64(payload)}.sig"


class TestRegisterFromAccessToken:
    def test_returns_credentials_and_type(self):
        exp = int(time.time()) + 86_400
        token = _make_jwt({"iss": "https://auth.openai.com", "exp": exp})
        creds, token_type = CodexCredentialAuthenticator().register_from_access_token(token)
        assert creds == {"access_token": token, "expires_at": exp}
        assert token_type == "codex_access_token"

    def test_rejects_non_three_segment(self):
        with pytest.raises(AuthenticationError, match="JWT"):
            CodexCredentialAuthenticator().register_from_access_token("not.a.jwt.token")

    def test_rejects_undecodable_payload(self):
        with pytest.raises(AuthenticationError):
            CodexCredentialAuthenticator().register_from_access_token("aaa.@@@.sig")

    def test_rejects_expired(self):
        token = _make_jwt({"iss": "https://auth.openai.com", "exp": int(time.time()) - 60})
        with pytest.raises(AuthenticationError, match="expired"):
            CodexCredentialAuthenticator().register_from_access_token(token)

    def test_warns_on_unexpected_issuer_but_accepts(self, caplog):
        # Codex mints tokens under multiple issuers (e.g. the
        # chatgpt.com/codex-backend agent-identity issuer). The strategy
        # should log a warning but still register the token — Codex
        # itself will reject genuinely invalid tokens on first use.
        exp = int(time.time()) + 86_400
        token = _make_jwt(
            {"iss": "https://chatgpt.com/codex-backend/agent-identity", "exp": exp}
        )
        with caplog.at_level(logging.WARNING, logger="agento.modules.codex.src.auth"):
            creds, token_type = CodexCredentialAuthenticator().register_from_access_token(token)
        assert creds == {"access_token": token, "expires_at": exp}
        assert token_type == "codex_access_token"
        assert any("Unexpected JWT issuer" in r.message for r in caplog.records)

    def test_rejects_missing_exp(self):
        token = _make_jwt({"iss": "https://auth.openai.com"})
        with pytest.raises(AuthenticationError, match="exp"):
            CodexCredentialAuthenticator().register_from_access_token(token)


class TestRegisterFromApiKey:
    def test_accepts_sk_prefix(self):
        creds, token_type = CodexCredentialAuthenticator().register_from_api_key("sk-proj-abc123")
        assert creds == {"api_key": "sk-proj-abc123"}
        assert token_type == "openai_api_key"

    def test_rejects_empty(self):
        with pytest.raises(AuthenticationError):
            CodexCredentialAuthenticator().register_from_api_key("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(AuthenticationError):
            CodexCredentialAuthenticator().register_from_api_key("   ")

    def test_rejects_obvious_non_openai_prefix(self):
        """Anthropic keys (sk-ant-...) must not be accepted as OpenAI keys."""
        with pytest.raises(AuthenticationError, match="OpenAI"):
            CodexCredentialAuthenticator().register_from_api_key("sk-ant-XXXX")

    def test_strips_surrounding_whitespace(self):
        creds, token_type = CodexCredentialAuthenticator().register_from_api_key("  sk-proj-abc  ")
        assert creds == {"api_key": "sk-proj-abc"}
        assert token_type == "openai_api_key"


class TestAccountLabel:
    """AG-45: the ChatGPT/OpenAI account e-mail, decoded from the id_token JWT."""

    def test_extracts_email_claim_flat_id_token(self):
        token = _make_jwt({"email": "dev@openai-user.com"})
        creds = {"subscription_key": "sk", "id_token": token}
        assert CodexCredentialAuthenticator().account_label(creds) == "dev@openai-user.com"

    def test_extracts_email_from_openai_profile_claim(self):
        token = _make_jwt({"https://api.openai.com/profile": {"email": "p@openai-user.com"}})
        creds = {"id_token": token}
        assert CodexCredentialAuthenticator().account_label(creds) == "p@openai-user.com"

    def test_reads_id_token_nested_in_raw_auth_tokens(self):
        token = _make_jwt({"email": "nested@openai-user.com"})
        creds = {"raw_auth": {"tokens": {"id_token": token}}}
        assert CodexCredentialAuthenticator().account_label(creds) == "nested@openai-user.com"

    def test_none_without_id_token(self):
        assert CodexCredentialAuthenticator().account_label({"access_token": "sk"}) is None

    def test_none_for_undecodable_id_token(self):
        assert CodexCredentialAuthenticator().account_label({"id_token": "aaa.@@@.sig"}) is None

    def test_none_when_no_email_claim(self):
        token = _make_jwt({"sub": "user-123"})
        assert CodexCredentialAuthenticator().account_label({"id_token": token}) is None
