"""Claude credential authenticator (interactive OAuth + API key)."""
from __future__ import annotations

import json
import logging
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
        real_home = str(Path.home())
        _run_cli(["claude"], real_home, "Claude")

        creds_path = Path(real_home) / ".claude" / ".credentials.json"
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
        claude_json_path = Path(real_home) / ".claude.json"
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
