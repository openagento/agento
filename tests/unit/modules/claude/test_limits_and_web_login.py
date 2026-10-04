"""Claude ``fetch_limits`` (respx double of the OAuth usage endpoint, F7) and
``start_web_login`` against a fake ``claude`` CLI on PATH."""
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
from agento.modules.claude.src.auth import ClaudeCredentialAuthenticator

USAGE = "https://api.anthropic.com/api/oauth/usage"
CREDS = {"subscription_key": "sk-ant-oat-x"}


@respx.mock
def test_two_windows_from_a_200():
    route = respx.get(USAGE).mock(return_value=httpx.Response(200, json={
        "five_hour": {"utilization": 42.0, "resets_at": "2026-10-04T15:00:00+00:00"},
        "seven_day": {"utilization": 7, "resets_at": "2026-10-09T00:00:00Z"},
    }))
    limits = ClaudeCredentialAuthenticator().fetch_limits(CREDS, "oauth")
    assert limits == CredentialLimits(windows=(
        LimitWindow("5h", 42.0, datetime(2026, 10, 4, 15, tzinfo=UTC)),
        LimitWindow("Week", 7.0, datetime(2026, 10, 9, tzinfo=UTC)),
    ))
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer sk-ant-oat-x"
    assert req.headers["anthropic-beta"] == "oauth-2025-04-20"


@respx.mock
@pytest.mark.parametrize("status", [401, 429])
def test_an_error_status_raises(status):
    respx.get(USAGE).mock(return_value=httpx.Response(status, json={"error": "x"}))
    with pytest.raises(httpx.HTTPStatusError):
        ClaudeCredentialAuthenticator().fetch_limits(CREDS, "oauth")


@respx.mock
def test_an_answer_without_windows_raises():
    respx.get(USAGE).mock(return_value=httpx.Response(200, json={"five_hour": {"resets_at": None}}))
    with pytest.raises((KeyError, ValueError)):
        ClaudeCredentialAuthenticator().fetch_limits(CREDS, "oauth")


@respx.mock
def test_an_api_key_has_no_limits_and_no_call():
    assert ClaudeCredentialAuthenticator().fetch_limits({"api_key": "k"}, "anthropic_api_key") is None
    assert respx.calls.call_count == 0


FAKE_CLAUDE = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys
    assert sys.argv[1:] == ["auth", "login", "--claudeai"], sys.argv
    print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&state=s1", flush=True)
    code = input("Paste code here if prompted > ")
    if code != "abc#s1":
        sys.exit(1)
    home = os.environ["HOME"]
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    json.dump({{"claudeAiOauth": {{"accessToken": "at", "refreshToken": "rt", "expiresAt": 1}}}},
              open(os.path.join(home, ".claude", ".credentials.json"), "w"))
    json.dump({{"oauthAccount": {{"emailAddress": "ops@example.com"}}}}, open(os.path.join(home, ".claude.json"), "w"))
""")


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(FAKE_CLAUDE)
    (bin_dir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    home = tmp_path / "home"
    home.mkdir()
    return str(home)


def _finish(login, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = login.poll()
        if result is not None:
            return result
        time.sleep(0.05)
    raise AssertionError("login did not finish")


def test_web_login_code_flow(fake_claude):
    login = ClaudeCredentialAuthenticator().start_web_login(fake_claude, logging.getLogger("t"))
    try:
        assert login.prompt.url == "https://claude.com/cai/oauth/authorize?code=true&state=s1"
        assert (login.prompt.needs_code, login.prompt.user_code) == (True, None)
        login.submit_code("abc#s1")
        result = _finish(login)
    finally:
        login.close()
    assert (result.subscription_key, result.refresh_token) == ("at", "rt")
    assert result.raw_auth["claude_json"] == {"oauthAccount": {"emailAddress": "ops@example.com"}}


def test_web_login_wrong_code_fails(fake_claude):
    login = ClaudeCredentialAuthenticator().start_web_login(fake_claude, logging.getLogger("t"))
    login.submit_code("wrong")
    with pytest.raises(AuthenticationError):
        _finish(login)
    login.close()


def test_web_login_without_a_url_raises(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(f"#!{sys.executable}\nprint('no url here')\n")
    (bin_dir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    with pytest.raises(AuthenticationError):
        ClaudeCredentialAuthenticator().start_web_login(str(tmp_path), logging.getLogger("t"))

