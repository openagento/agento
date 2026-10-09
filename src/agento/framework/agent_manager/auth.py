"""Interactive authentication for agent CLI tools, keyed by credential scope.

Runs a harness CLI login in a runner, in an isolated temporary HOME on the shared
/workspace, then extracts and normalises credentials to the internal JSON format.

The registry itself lives in :mod:`agento.framework.harness.registry` — authenticators
are keyed by ``credential_scope``, which is what partitions the credential pool, so
``credential:register <scope>`` never has to work out which harness owns a scope.
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from pathlib import Path

from ..harness.protocols import AuthResult, InteractiveLogin
from ..harness.registry import get_authenticator, list_credential_scopes
from ..runner import client
from .errors import AuthenticationError

__all__ = [
    "AuthResult",
    "AuthenticationError",
    "attended_login",
    "authenticate_interactive",
    "credentials_from_auth",
    "get_available_scopes",
    "save_credentials",
]


def get_available_scopes() -> list[str]:
    """Credential scopes that a registered harness can authenticate."""
    return list_credential_scopes()


def authenticate_interactive(
    scope: str,
    logger: logging.Logger | None = None,
) -> AuthResult:
    """Run interactive OAuth for the given credential scope.

    Creates an isolated temp HOME on the shared /workspace (the runner sees it at the same
    path), so the auth flow does NOT touch ``~/.claude`` or ``~/.codex``.

    Raises :class:`AuthenticationError` on failure or user cancellation.
    """
    _log = logger or logging.getLogger(__name__)

    authenticator = get_authenticator(scope)
    if authenticator is None:
        raise ValueError(
            f"No authenticator registered for credential scope {scope!r}. "
            f"Available: {list_credential_scopes()}"
        )

    tmp_home = tempfile.mkdtemp(prefix=f"auth_{scope}_", dir=client.shared_tmp())
    _log.info(f"Using isolated HOME: {tmp_home}")

    try:
        return authenticator.authenticate_interactive(tmp_home, _log)
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)
        _log.debug(f"Cleaned up temp HOME: {tmp_home}")


def credentials_from_auth(auth_result: AuthResult) -> dict:
    """The stored ``oauth`` credential payload of an interactive login: one shape for every
    caller (``credential:register``, ``credential:refresh``, the panel re-login worker)."""
    return {
        "subscription_key": auth_result.subscription_key,
        "refresh_token": auth_result.refresh_token,
        "expires_at": auth_result.expires_at,
        "subscription_type": auth_result.subscription_type,
        "id_token": auth_result.id_token,
        "raw_auth": auth_result.raw_auth,
    }


def save_credentials(auth_result: AuthResult, output_path: str) -> None:
    """Save normalised credentials to a JSON file. Creates parent dirs if needed."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(credentials_from_auth(auth_result), indent=2))


def attended_login(login: InteractiveLogin) -> AuthResult:
    """Finish a ``start_web_login`` on the operator's terminal (``credential:register``).
    The vendor CLI runs in a runner, never in this container (SEC-1)."""
    try:
        print(f"Open this URL in your browser:\n  {login.prompt.url}")
        if login.prompt.user_code:
            print(f"Enter this code there: {login.prompt.user_code}")
        if login.prompt.needs_code:
            login.submit_code(input("Paste the code from the login page: ").strip())
        while (result := login.poll()) is None:
            time.sleep(0.5)
        return result
    finally:
        login.close()
