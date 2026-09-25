from __future__ import annotations

import pytest

from agento.web import security

ORIGINS = security.Origins(panel="https://panel.localhost:8443", apps="https://apps.localhost:8443")
LAUNCH_ID = "0123456789abcdef0123456789abcdef"


def _attrs(cookie: str) -> set[str]:
    return {part.strip().split("=")[0] for part in cookie.split(";")[1:]}


@pytest.mark.parametrize("cookie", [
    security.session_cookie("tok", 60),
    security.launch_cookie(LAUNCH_ID, "tok", 60),
    security.clear_cookie(security.SESSION_COOKIE),
])
def test_cookies_carry_every_attribute_explicitly(cookie):
    assert cookie.startswith("__Host-")
    assert {"Secure", "HttpOnly", "SameSite", "Path", "Max-Age"} <= _attrs(cookie)
    assert "Domain" not in _attrs(cookie)
    assert "Path=/;" in cookie


def test_same_site_values():
    assert "SameSite=Strict" in security.session_cookie("t", 1)
    assert "SameSite=Lax" in security.launch_cookie(LAUNCH_ID, "t", 1)


def test_launch_cookie_names_are_distinct_and_hex_only():
    other = "f" * 32
    assert security.launch_cookie_name(LAUNCH_ID) != security.launch_cookie_name(other)
    for bad in ("../x", "ABCDEF0123456789ABCDEF0123456789", "short"):
        with pytest.raises(ValueError):
            security.launch_cookie_name(bad)


def test_parse_cookies_and_launch_cookies():
    header = f"__Host-agento-session=s1; __Host-agento-launch-{LAUNCH_ID}=l1; __Host-agento-launch-bad=x"
    cookies = security.parse_cookies(header)
    assert cookies[security.SESSION_COOKIE] == "s1"
    assert security.launch_cookies(cookies) == {LAUNCH_ID: "l1"}
    assert security.parse_cookies(None) == {}
    assert security.parse_cookies('a="unterminated') == {}


def test_origins_from_env(monkeypatch):
    monkeypatch.setenv("AGENTO_PANEL_HOST", "panel.example.com")
    monkeypatch.setenv("AGENTO_APPS_HOST", "apps.example.com")
    monkeypatch.setenv("AGENTO_PROXY_PORT", "443")
    assert security.Origins.from_env() == security.Origins("https://panel.example.com", "https://apps.example.com")


def test_write_allowed():
    ok = {"Origin": ORIGINS.panel, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}
    assert security.write_allowed(ok, ORIGINS)
    assert security.write_allowed({"Origin": ORIGINS.panel}, ORIGINS)
    for bad in (
        {},
        {**ok, "Origin": ORIGINS.apps},
        {**ok, "Origin": "https://x.share.localhost:8443"},
        {**ok, "Origin": "null"},
        {**ok, "Sec-Fetch-Site": "same-site"},
        {**ok, "Sec-Fetch-Site": "cross-site"},
        {**ok, "Sec-Fetch-Mode": "navigate"},
        {**ok, "Sec-Fetch-Mode": "no-cors"},
    ):
        assert not security.write_allowed(bad, ORIGINS)
