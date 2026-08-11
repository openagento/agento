"""Codex device-auth authentication strategy."""
from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

from agento.framework.agent_manager.auth import (
    AuthenticationError,
    AuthResult,
    _run_cli,
)
from agento.framework.harness import (
    CredentialRegistrationMode,
    UnsupportedRegistrationMode,
)

_OPENAI_ISSUER = "https://auth.openai.com"

_logger = logging.getLogger(__name__)


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except Exception as exc:
        raise AuthenticationError(f"Invalid JWT segment: {exc}") from exc


class CodexCredentialAuthenticator:
    """Run ``codex auth login --device-auth`` in isolated HOME, extract credentials."""

    def authenticate_interactive(self, tmp_home: str, logger: logging.Logger) -> AuthResult:
        logger.info("Starting Codex device-auth login (follow the URL in your browser)...")
        _run_cli(["codex", "auth", "login", "--device-auth"], tmp_home, "Codex")

        creds_path = Path(tmp_home) / ".codex" / "auth.json"
        if not creds_path.is_file():
            raise AuthenticationError(
                "Codex login completed but auth.json not found. "
                "Auth may have been cancelled."
            )

        raw = json.loads(creds_path.read_text())
        tokens = raw.get("tokens", {})
        access_token = tokens.get("access_token")
        if not access_token:
            raise AuthenticationError(
                "Codex auth.json exists but contains no access_token. "
                "Auth may have been incomplete."
            )

        return AuthResult(
            subscription_key=access_token,
            refresh_token=tokens.get("refresh_token"),
            expires_at=None,
            subscription_type=None,
            id_token=tokens.get("id_token"),
            raw_auth=raw,
        )

    def register_from_access_token(self, token: str) -> tuple[dict, str]:
        """Validate a Codex/OpenAI access-token JWT and return
        (credentials, type) for persistence.

        Validates JWT shape and expiry. Warns (does not reject) when the
        issuer differs from the canonical OpenAI issuer — Codex mints
        tokens under several issuers (e.g. chatgpt.com/codex-backend/...).
        Signature is NOT verified (Codex CLI does that on first use)."""
        if not isinstance(token, str) or token.count(".") != 2:
            raise AuthenticationError(
                "Access token is not a JWT (expected 3 dot-separated segments)."
            )
        _hdr, payload_b64, _sig = token.split(".")
        try:
            payload = json.loads(_b64url_decode(payload_b64))
        except json.JSONDecodeError as exc:
            raise AuthenticationError(f"JWT payload is not valid JSON: {exc}") from exc

        iss = payload.get("iss")
        if iss != _OPENAI_ISSUER:
            _logger.warning(
                "Unexpected JWT issuer: %r (expected %r). Continuing — Codex "
                "will reject the token on first use if it is not actually valid.",
                iss, _OPENAI_ISSUER,
            )
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            raise AuthenticationError("JWT payload missing numeric 'exp' claim.")
        if exp <= time.time():
            raise AuthenticationError(f"Access token is already expired (exp={exp}).")

        return {"access_token": token, "expires_at": int(exp)}, "codex_access_token"

    def register_from_api_key(self, key: str) -> tuple[dict, str]:
        """Validate an OpenAI API key string and return (credentials, type)
        for persistence."""
        if not isinstance(key, str) or not key.strip():
            raise AuthenticationError("OpenAI API key is empty.")
        stripped = key.strip()
        if stripped.startswith("sk-ant-"):
            raise AuthenticationError(
                "Refusing to register an Anthropic key (sk-ant-...) as an OpenAI key."
            )
        return {"api_key": stripped}, "openai_api_key"

    def register_from_secret(
        self, mode: CredentialRegistrationMode, secret: str
    ) -> tuple[dict, str]:
        """Total dispatch — the registry validates ``mode`` against the declared
        ``registration_modes`` first, so the raise is a defensive contract only."""
        if mode is CredentialRegistrationMode.API_KEY:
            return self.register_from_api_key(secret)
        if mode is CredentialRegistrationMode.ACCESS_TOKEN:
            return self.register_from_access_token(secret)
        raise UnsupportedRegistrationMode(
            f"Codex does not support registration mode {mode.value!r}"
        )
