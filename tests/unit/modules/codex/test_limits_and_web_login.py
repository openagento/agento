"""Codex ``fetch_limits`` (respx double of the ChatGPT usage endpoint, F6) and
``start_web_login`` (device flow) against a fake ``codex`` CLI on PATH."""
from __future__ import annotations

import logging
import sys
import textwrap
import time
from datetime import UTC, datetime

import httpx
import pytest
import respx

from agento.framework.agent_manager.errors import AuthenticationError
from agento.framework.harness.protocols import CredentialLimits, LimitWindow
from agento.modules.codex.src.auth import CodexCredentialAuthenticator

# The CLI runs in a runner (WS5): an in-process server here.
pytestmark = pytest.mark.usefixtures("runner_server")

USAGE = "https://chatgpt.com/backend-api/wham/usage"
CREDS = {"subscription_key": "at-x", "raw_auth": {"tokens": {"account_id": "acc-1"}}}


def _window(used, reset, secs):
    return {"used_percent": used, "reset_at": reset, "limit_window_seconds": secs}


@respx.mock
def test_two_windows_from_a_200():
    route = respx.get(USAGE).mock(return_value=httpx.Response(200, json={"rate_limit": {
        "primary_window": _window(12.5, 1_791_126_000, 18000),
        "secondary_window": _window(40, 1_791_500_000, 604800),
    }}))
    limits = CodexCredentialAuthenticator().fetch_limits(CREDS, "oauth")
    assert limits == CredentialLimits(windows=(
        LimitWindow("5h", 12.5, datetime.fromtimestamp(1_791_126_000, UTC)),
        LimitWindow("Week", 40.0, datetime.fromtimestamp(1_791_500_000, UTC)),
    ))
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer at-x"
    assert req.headers["ChatGPT-Account-Id"] == "acc-1"
    # chatgpt.com refuses the default python-httpx agent (live check, 2026-10-05).
    assert not req.headers["User-Agent"].startswith("python-httpx")
    assert req.headers["Accept"] == "application/json"


@respx.mock
def test_an_unknown_window_length_is_named_in_hours():
    respx.get(USAGE).mock(return_value=httpx.Response(200, json={"rate_limit": {
        "primary_window": _window(1, None, 7200), "secondary_window": None}}))
    limits = CodexCredentialAuthenticator().fetch_limits(CREDS, "oauth")
    assert limits.windows == (LimitWindow("2h", 1.0, None),)


@respx.mock
@pytest.mark.parametrize("status", [401, 429])
def test_an_error_status_raises(status):
    respx.get(USAGE).mock(return_value=httpx.Response(status))
    with pytest.raises(httpx.HTTPStatusError):
        CodexCredentialAuthenticator().fetch_limits(CREDS, "oauth")


@respx.mock
def test_a_missing_field_raises():
    respx.get(USAGE).mock(return_value=httpx.Response(200, json={"plan_type": "plus"}))
    with pytest.raises(KeyError):
        CodexCredentialAuthenticator().fetch_limits(CREDS, "oauth")


@respx.mock
def test_no_account_id_raises_before_a_call():
    with pytest.raises((KeyError, TypeError)):
        CodexCredentialAuthenticator().fetch_limits({"subscription_key": "x", "raw_auth": {}}, "oauth")
    assert respx.calls.call_count == 0


@respx.mock
@pytest.mark.parametrize("ctype", ["openai_api_key", "codex_access_token"])
def test_no_limits_without_an_oauth_account(ctype):
    assert CodexCredentialAuthenticator().fetch_limits({"api_key": "k"}, ctype) is None
    assert respx.calls.call_count == 0


FAKE_CODEX = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys, time
    assert sys.argv[1:] == ["login", "--device-auth"], sys.argv
    print("Follow these steps to sign in with ChatGPT using device code authorization:")
    print("1. Open this link in your browser and sign in to your account")
    print("   https://auth.openai.com/codex/device")
    print("2. Enter this one-time code (expires in 15 minutes)")
    print("   ABCD-EFGHI", flush=True)
    time.sleep(0.5)
    home = os.environ["HOME"]
    if os.path.exists(os.path.join(home, "fail")):
        sys.exit(1)
    os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
    json.dump({{"tokens": {{"access_token": "at", "refresh_token": "rt", "id_token": "a.b.c",
                            "account_id": "acc-1"}}}},
              open(os.path.join(home, ".codex", "auth.json"), "w"))
""")


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "codex").write_text(FAKE_CODEX)
    (bin_dir / "codex").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    home = tmp_path / "home"
    home.mkdir()
    return str(home)


def _finish(login, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (result := login.poll()) is not None:
            return result
        time.sleep(0.05)
    raise AssertionError("login did not finish")


def test_web_login_device_flow(fake_codex):
    login = CodexCredentialAuthenticator().start_web_login(fake_codex, logging.getLogger("t"))
    try:
        assert login.prompt.url == "https://auth.openai.com/codex/device"
        assert (login.prompt.user_code, login.prompt.needs_code) == ("ABCD-EFGHI", False)
        result = _finish(login)
    finally:
        login.close()
    assert (result.subscription_key, result.refresh_token) == ("at", "rt")
    assert result.raw_auth["tokens"]["account_id"] == "acc-1"


def test_web_login_cli_failure(fake_codex):
    open(f"{fake_codex}/fail", "w").close()
    login = CodexCredentialAuthenticator().start_web_login(fake_codex, logging.getLogger("t"))
    with pytest.raises(AuthenticationError):
        _finish(login)
    login.close()
