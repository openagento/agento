"""Tests for PiOpenRouterAuthenticator."""
from __future__ import annotations

from agento.modules.pi.src.auth import PiOpenRouterAuthenticator


class TestAccountLabel:
    def test_openrouter_api_key_has_no_account(self):
        assert PiOpenRouterAuthenticator().account_label({"api_key": "sk-or-x"}) is None
