"""Pi/OpenRouter ``fetch_limits``: the credit balance (respx double of F9)."""
from __future__ import annotations

import httpx
import pytest
import respx

from agento.framework.harness.protocols import CredentialLimits
from agento.modules.pi.src.auth import PiOpenRouterAuthenticator

CREDITS = "https://openrouter.ai/api/v1/credits"


@respx.mock
def test_balance_is_credits_minus_usage():
    route = respx.get(CREDITS).mock(return_value=httpx.Response(
        200, json={"data": {"total_credits": 20, "total_usage": 7.66}}))
    limits = PiOpenRouterAuthenticator().fetch_limits({"api_key": "sk-or-x"}, "openrouter_api_key")
    assert limits == CredentialLimits(balance_usd=pytest.approx(12.34))
    assert route.calls.last.request.headers["Authorization"] == "Bearer sk-or-x"


@respx.mock
@pytest.mark.parametrize("status", [401, 429])
def test_an_error_status_raises(status):
    respx.get(CREDITS).mock(return_value=httpx.Response(status))
    with pytest.raises(httpx.HTTPStatusError):
        PiOpenRouterAuthenticator().fetch_limits({"api_key": "k"}, "openrouter_api_key")


@respx.mock
def test_a_missing_field_raises():
    respx.get(CREDITS).mock(return_value=httpx.Response(200, json={"data": {"total_credits": 1}}))
    with pytest.raises(KeyError):
        PiOpenRouterAuthenticator().fetch_limits({"api_key": "k"}, "openrouter_api_key")


@respx.mock
def test_another_type_has_no_limits():
    assert PiOpenRouterAuthenticator().fetch_limits({"api_key": "k"}, "other") is None
    assert respx.calls.call_count == 0
