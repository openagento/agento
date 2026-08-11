"""Interactive authentication for agent CLI tools, keyed by credential scope.

Launches a harness CLI in an isolated temporary HOME directory to perform OAuth,
then extracts and normalises credentials to the internal JSON format.

The isolation prevents the auth flow from overwriting the main active credentials at
``/workspace/.claude`` or ``/workspace/.codex``.

The registry itself lives in :mod:`agento.framework.harness.registry` — authenticators
are keyed by ``credential_scope``, which is what partitions the credential pool, so
``credential:register <scope>`` never has to work out which harness owns a scope.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..harness.protocols import AuthResult
from ..harness.registry import get_authenticator, list_credential_scopes
from .errors import AuthenticationError

__all__ = [
    "AuthResult",
    "AuthenticationError",
    "authenticate_interactive",
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

    Creates an isolated temp HOME directory so the auth flow does NOT touch
    ``~/.claude`` or ``~/.codex`` (symlinked to ``/workspace/``).

    Raises :class:`AuthenticationError` on failure or user cancellation.
    """
    _log = logger or logging.getLogger(__name__)

    authenticator = get_authenticator(scope)
    if authenticator is None:
        raise ValueError(
            f"No authenticator registered for credential scope {scope!r}. "
            f"Available: {list_credential_scopes()}"
        )

    tmp_home = tempfile.mkdtemp(prefix=f"auth_{scope}_")
    _log.info(f"Using isolated HOME: {tmp_home}")

    try:
        return authenticator.authenticate_interactive(tmp_home, _log)
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)
        _log.debug(f"Cleaned up temp HOME: {tmp_home}")


def save_credentials(auth_result: AuthResult, output_path: str) -> None:
    """Save normalised credentials to a JSON file. Creates parent dirs if needed."""
    data = {
        "subscription_key": auth_result.subscription_key,
        "refresh_token": auth_result.refresh_token,
        "expires_at": auth_result.expires_at,
        "subscription_type": auth_result.subscription_type,
        "id_token": auth_result.id_token,
        "raw_auth": auth_result.raw_auth,
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Shared CLI helper (used by authenticator implementations in harness modules)
# ---------------------------------------------------------------------------

def _run_cli(cmd: list[str], tmp_home: str, name: str) -> None:
    """Run a CLI command with isolated HOME. Raises on failure."""
    env = {**os.environ, "HOME": tmp_home}
    try:
        proc = subprocess.run(cmd, env=env)
    except FileNotFoundError as exc:
        raise AuthenticationError(f"{name} CLI not found. Is it installed?") from exc

    if proc.returncode != 0:
        raise AuthenticationError(
            f"{name} login failed with exit code {proc.returncode}"
        )
