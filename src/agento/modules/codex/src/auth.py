"""Codex device-auth authentication strategy."""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from agento.framework.agent_manager.auth import (
    AuthenticationError,
    AuthResult,
    _run_cli,
)
from agento.framework.agent_manager.pty_login import PtyLogin, spawn
from agento.framework.harness import (
    CredentialLimits,
    CredentialRegistrationMode,
    LimitWindow,
    LoginPrompt,
    UnsupportedRegistrationMode,
)

_OPENAI_ISSUER = "https://auth.openai.com"
# Not verified live here (plan ASSUMPTION A1): any failure shows no limits.
_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_WINDOW_LABELS = {18000: "5h", 604800: "Week"}
_DEVICE_URL = re.compile(r"https://auth\.openai\.com/\S+")
_DEVICE_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")

_logger = logging.getLogger(__name__)


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except Exception as exc:
        raise AuthenticationError(f"Invalid JWT segment: {exc}") from exc


def _read_login(home: Path) -> AuthResult:
    """The credential the Codex CLI wrote into ``home`` after a login."""
    creds_path = home / ".codex" / "auth.json"
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


def _limit_window(window: dict) -> LimitWindow:
    seconds = int(window["limit_window_seconds"])
    reset_at = window.get("reset_at")
    return LimitWindow(
        label=_WINDOW_LABELS.get(seconds, f"{seconds // 3600}h"),
        used_pct=float(window["used_percent"]),
        resets_at=datetime.fromtimestamp(reset_at, UTC) if reset_at is not None else None,
    )


class CodexCredentialAuthenticator:
    """Run ``codex auth login --device-auth`` in isolated HOME, extract credentials."""

    def authenticate_interactive(self, tmp_home: str, logger: logging.Logger) -> AuthResult:
        logger.info("Starting Codex device-auth login (follow the URL in your browser)...")
        _run_cli(["codex", "auth", "login", "--device-auth"], tmp_home, "Codex")
        return _read_login(Path(tmp_home))

    def start_web_login(self, tmp_home: str, logger: logging.Logger) -> PtyLogin:
        """``codex login --device-auth`` in ``tmp_home``: it prints the device page and a
        one-time code, then waits until the operator signs in there."""
        proc = spawn(["codex", "login", "--device-auth"], tmp_home)
        url = proc.read_until(_DEVICE_URL, timeout=20)
        code = proc.read_until(_DEVICE_CODE, timeout=10) if url else None
        if url is None or code is None:
            proc.kill()
            raise AuthenticationError("The Codex CLI printed no device login URL and code.")
        prompt = LoginPrompt(url=url.group(0), user_code=code.group(0), needs_code=False)
        return PtyLogin(proc, prompt, lambda: _read_login(Path(tmp_home)))

    def fetch_limits(self, credentials: dict, credential_type: str) -> CredentialLimits | None:
        """The 5 h and week use of a ChatGPT subscription (OAuth) credential.

        A ``codex_access_token`` has no account id verified here, so it shows no limits
        (ROADMAP.md), like an API key."""
        if credential_type != "oauth":
            return None
        account_id = credentials["raw_auth"]["tokens"]["account_id"]
        resp = httpx.get(
            _USAGE_URL,
            headers={
                "Authorization": f"Bearer {credentials['subscription_key']}",
                "ChatGPT-Account-Id": account_id,
            },
            timeout=10,
        )
        resp.raise_for_status()
        rate_limit = resp.json()["rate_limit"]
        windows = tuple(
            _limit_window(rate_limit[key])
            for key in ("primary_window", "secondary_window")
            if isinstance(rate_limit.get(key), dict)
        )
        if not windows:
            raise ValueError("the usage answer has no window")
        return CredentialLimits(windows=windows)

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

    def account_label(self, credentials: dict) -> str | None:
        """The ChatGPT/OpenAI account e-mail behind a Codex credential.

        Read from the ``id_token`` JWT's ``email`` claim (OpenAI also nests it under the
        ``https://api.openai.com/profile`` claim). The device-auth flow stores the
        ``id_token`` both flat on the payload and inside ``raw_auth.tokens``; both are
        checked. Returns ``None`` when there is no id_token (access-token / API-key
        credentials) or it cannot be decoded."""
        id_token = credentials.get("id_token")
        if not id_token:
            raw_auth = credentials.get("raw_auth")
            tokens = raw_auth.get("tokens") if isinstance(raw_auth, dict) else None
            id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
        if not isinstance(id_token, str) or id_token.count(".") != 2:
            return None
        try:
            payload = json.loads(_b64url_decode(id_token.split(".")[1]))
        except (AuthenticationError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        email = payload.get("email")
        if not email:
            profile = payload.get("https://api.openai.com/profile")
            email = profile.get("email") if isinstance(profile, dict) else None
        return email if isinstance(email, str) and email.strip() else None
