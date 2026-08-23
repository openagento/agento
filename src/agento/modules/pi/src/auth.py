"""OpenRouter credential registration for the Pi harness.

OpenRouter needs only an API key in the environment — no ``auth.json``, no ``/login``
flow. So the interactive path does not exist and says so, rather than pretending.
"""
from __future__ import annotations

import logging

from agento.framework.agent_manager.auth import AuthenticationError, AuthResult
from agento.framework.harness import (
    CredentialRegistrationMode,
    UnsupportedRegistrationMode,
)

CREDENTIAL_TYPE = "openrouter_api_key"


class PiOpenRouterAuthenticator:
    """Registers an OpenRouter API key. Delivery is via env, never disk."""

    def authenticate_interactive(self, tmp_home: str, logger: logging.Logger) -> AuthResult:
        raise UnsupportedRegistrationMode(
            "OpenRouter has no interactive login. Register the key instead:\n"
            "  echo \"$OPENROUTER_API_KEY\" | agento credential:register openrouter "
            "<label> --with-api-key"
        )

    def register_from_api_key(self, secret: str) -> tuple[dict, str]:
        key = (secret or "").strip()
        if not key:
            raise AuthenticationError("OpenRouter API key is empty.")
        return {"api_key": key}, CREDENTIAL_TYPE

    def register_from_secret(
        self, mode: CredentialRegistrationMode, secret: str
    ) -> tuple[dict, str]:
        """Total dispatch. The registry validates ``mode`` against the declared
        ``registration_modes`` first, so the raise is a defensive contract only."""
        if mode is CredentialRegistrationMode.API_KEY:
            return self.register_from_api_key(secret)
        raise UnsupportedRegistrationMode(
            f"Pi/OpenRouter does not support registration mode {mode.value!r}"
        )
