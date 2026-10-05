"""Claude credential authenticator (interactive OAuth + API key)."""
from __future__ import annotations

import json
import logging
import re
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

# Undocumented by Anthropic (plan ASSUMPTION A2): any failure shows no limits.
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_USAGE_WINDOWS = (("five_hour", "5h"), ("seven_day", "Week"))
_LOGIN_URL = re.compile(r"https://\S+/oauth/authorize\?\S+")


def _read_login(home: Path) -> AuthResult:
    """The credential the Claude CLI wrote into ``home`` after a login."""
    creds_path = home / ".claude" / ".credentials.json"
    if not creds_path.is_file():
        raise AuthenticationError(
            "Claude login completed but credentials file not found. "
            "Auth may have been cancelled."
        )

    raw = json.loads(creds_path.read_text())
    oauth = raw.get("claudeAiOauth", {})
    access_token = oauth.get("accessToken")
    if not access_token:
        raise AuthenticationError(
            "Credentials file exists but contains no accessToken. "
            "Auth may have been incomplete."
        )

    # Claude Code stores its login state in TWO places:
    # - ``~/.claude/.credentials.json`` (oauth tokens; seen above)
    # - ``~/.claude.json`` at HOME root (``oauthAccount`` + per-install user state)
    # Without the second, a sandboxed Claude with HOME=<build dir> sees creds but
    # still falls through to the login picker. Capture both so ``write_credentials``
    # can restore them verbatim.
    claude_json_path = home / ".claude.json"
    claude_json: dict = {}
    if claude_json_path.is_file():
        try:
            claude_json = json.loads(claude_json_path.read_text())
            if not isinstance(claude_json, dict):
                claude_json = {}
        except (json.JSONDecodeError, OSError):
            claude_json = {}

    return AuthResult(
        subscription_key=access_token,
        refresh_token=oauth.get("refreshToken"),
        expires_at=oauth.get("expiresAt"),
        subscription_type=oauth.get("subscriptionType"),
        raw_auth={
            "credentials": raw,
            "claude_json": claude_json,
        },
    )


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC) if value else None


class ClaudeCredentialAuthenticator:
    """Run ``claude auth login`` with the user's real HOME.

    Claude CLI's OAuth polling depends on state in ``$HOME/.claude/``.
    An isolated temp HOME breaks the polling, so we ignore ``tmp_home``
    and use the real HOME for the CLI process.
    """

    def authenticate_interactive(self, tmp_home: str, logger: logging.Logger) -> AuthResult:
        logger.info("Starting Claude OAuth login (follow the URL in your browser)...")
        # Run full `claude` TUI (not `claude auth login`) — only the TUI
        # has the "Paste code here" prompt needed for headless/Docker auth.
        # Use real HOME because Claude CLI's OAuth polling needs $HOME/.claude/.
        # --strict-mcp-config with no --mcp-config: a login connects to no MCP server
        # (no project .mcp.json, no claude.ai connectors).
        real_home = str(Path.home())
        _run_cli(["claude", "--strict-mcp-config"], real_home, "Claude")
        return _read_login(Path(real_home))

    def start_web_login(self, tmp_home: str, logger: logging.Logger) -> PtyLogin:
        """``claude auth login --claudeai`` in ``tmp_home``: it prints the login URL and
        waits for the code the login page shows."""
        proc = spawn(["claude", "auth", "login", "--claudeai"], tmp_home)
        m = proc.read_until(_LOGIN_URL, timeout=20)
        if m is None:
            proc.kill()
            raise AuthenticationError("The Claude CLI printed no login URL.")
        prompt = LoginPrompt(url=m.group(0), user_code=None, needs_code=True)
        return PtyLogin(proc, prompt, lambda: _read_login(Path(tmp_home)))

    def fetch_limits(self, credentials: dict, credential_type: str) -> CredentialLimits | None:
        """The 5 h and week utilization of a Claude subscription (OAuth) credential."""
        if credential_type != "oauth":
            return None
        resp = httpx.get(
            _USAGE_URL,
            headers={
                "Authorization": f"Bearer {credentials['subscription_key']}",
                "anthropic-beta": "oauth-2025-04-20",
                "Accept": "application/json",
                # The OAuth usage endpoint answers only a Claude Code client (as CodexBar sends it).
                "User-Agent": "claude-code/2.1.0",
            },
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        windows = tuple(
            LimitWindow(label, float(body[key]["utilization"]), _parse_time(body[key].get("resets_at")))
            for key, label in _USAGE_WINDOWS
            if isinstance(body.get(key), dict)
        )
        if not windows:
            raise ValueError("the usage answer has no window")
        return CredentialLimits(windows=windows)

    def register_from_api_key(self, key: str) -> tuple[dict, str]:
        """Validate an Anthropic API key and return (credentials, type)
        for persistence."""
        if not isinstance(key, str) or not key.strip():
            raise AuthenticationError("Anthropic API key is empty.")
        stripped = key.strip()
        if stripped.startswith("sk-proj-") or stripped.startswith("sk-svcacct-"):
            raise AuthenticationError(
                "Refusing to register an OpenAI key (sk-proj-... / sk-svcacct-...) as an Anthropic key."
            )
        return {"api_key": stripped}, "anthropic_api_key"

    def register_from_secret(
        self, mode: CredentialRegistrationMode, secret: str
    ) -> tuple[dict, str]:
        """Total dispatch — the registry validates ``mode`` against the declared
        ``registration_modes`` first, so the raise is a defensive contract only."""
        if mode is CredentialRegistrationMode.API_KEY:
            return self.register_from_api_key(secret)
        raise UnsupportedRegistrationMode(
            f"Claude does not support registration mode {mode.value!r}"
        )

    def account_label(self, credentials: dict) -> str | None:
        """The Claude account e-mail captured at OAuth time.

        Claude Code records the logged-in account in ``~/.claude.json`` under
        ``oauthAccount.emailAddress``; ``authenticate_interactive`` copies that file
        verbatim into ``raw_auth.claude_json``. Returns ``None`` for API-key credentials
        (no OAuth account) or a payload missing the field, so the caller shows the
        account as unknown rather than guessing."""
        raw_auth = credentials.get("raw_auth")
        if not isinstance(raw_auth, dict):
            return None
        claude_json = raw_auth.get("claude_json")
        if not isinstance(claude_json, dict):
            return None
        account = claude_json.get("oauthAccount")
        if not isinstance(account, dict):
            return None
        email = account.get("emailAddress")
        return email if isinstance(email, str) and email.strip() else None
