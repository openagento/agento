from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from agento.framework.access import accounts, sessions
from agento.framework.bootstrap import CORE_MODULES_DIR
from agento.framework.module_loader import parse_module_dir
from agento.web import api, server
from tests.module_writer import write_module

from .conftest import APPS, TOKEN, a_session, panel_headers, sign_in_as

ALICE = accounts.User(id=1, username="alice", role="user", is_active=True)
PASSWORD = "correct horse battery"


@pytest.fixture
def signed_in(monkeypatch):
    sign_in_as(monkeypatch, ALICE)
    revoked = []
    monkeypatch.setattr(sessions, "revoke_session", lambda conn, sid: revoked.append(sid))
    return revoked


@pytest.fixture
def auth(monkeypatch):
    calls = []

    def sign_in(conn, username, password):
        calls.append(username)
        return (a_session(ALICE), TOKEN) if (username, password) == ("alice", PASSWORD) else None

    monkeypatch.setattr(sessions, "sign_in", sign_in)
    return calls


def _login(web, username="alice", password=PASSWORD, **headers):
    return httpx.post(f"{web}/api/session", json={"username": username, "password": password},
                      headers=panel_headers(**headers))


def test_login_sets_the_cookie_and_returns_a_valid_csrf_token(web, auth):
    r = _login(web)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"__Host-agento-session={TOKEN};")
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Strict" in cookie
    assert sessions.csrf_valid(TOKEN, r.json()["csrf_token"])
    assert r.json()["user"]["username"] == "alice"
    assert r.headers["cache-control"] == "no-store"


def test_bad_credentials_get_one_body(web, auth):
    wrong = _login(web, password="wrong password!!")
    unknown = _login(web, username="nobody")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"error": "invalid credentials", "code": "AUTH_INVALID_CREDENTIALS"}


def test_username_outside_the_grammar_is_not_queried_nor_throttled(web, auth):
    for _ in range(12):
        assert _login(web, username="Alice").status_code == 401
    assert auth == []
    assert _login(web).status_code == 200


def test_eleventh_failed_login_is_throttled(web, auth):
    for _ in range(10):
        assert _login(web, password="wrong password!!").status_code == 401
    assert _login(web).status_code == 429


def test_login_write_guard(web, auth):
    assert _login(web, Origin=APPS).status_code == 403
    assert _login(web, **{"Sec-Fetch-Mode": "navigate"}).status_code == 403
    r = httpx.post(f"{web}/api/session", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 403
    assert auth == []


def test_login_requires_json(web, auth):
    r = httpx.post(f"{web}/api/session", content=b"username=alice", headers={
        **panel_headers(), "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400
    r = httpx.post(f"{web}/api/session", content=b"{", headers={**panel_headers(), "Content-Type": "application/json"})
    assert r.status_code == 400
    r = httpx.post(f"{web}/api/session", content=b"x" * (server.MAX_JSON_BODY + 1),
                   headers={**panel_headers(), "Content-Type": "application/json"})
    assert r.status_code == 413


def test_get_session_needs_the_cookie(web, signed_in):
    assert httpx.get(f"{web}/api/session").status_code == 401
    r = httpx.get(f"{web}/api/session", cookies={"__Host-agento-session": TOKEN})
    assert r.status_code == 200
    assert sessions.csrf_valid(TOKEN, r.json()["csrf_token"])


def test_logout_needs_csrf(web, signed_in):
    cookies = {"__Host-agento-session": TOKEN}
    assert httpx.delete(f"{web}/api/session", cookies=cookies, headers=panel_headers()).status_code == 403
    bad = panel_headers(**{"X-CSRF-Token": sessions.csrf_token("another-session")})
    assert httpx.delete(f"{web}/api/session", cookies=cookies, headers=bad).status_code == 403
    assert signed_in == []
    good = panel_headers(**{"X-CSRF-Token": sessions.csrf_token(TOKEN)})
    r = httpx.delete(f"{web}/api/session", cookies=cookies, headers=good)
    assert r.status_code == 204
    assert "Max-Age=0" in r.headers["set-cookie"]
    assert signed_in == ["sid"]


def test_options_is_refused_without_cors_headers(web):
    r = httpx.request("OPTIONS", f"{web}/api/session", headers={
        "Origin": APPS, "Access-Control-Request-Method": "POST"})
    assert r.status_code == 405
    assert not [h for h in r.headers if h.lower().startswith("access-control-")]


def test_no_route_sets_a_cors_header(web, auth, signed_in):
    cookies = {"__Host-agento-session": TOKEN}
    for route in api.ROUTES:
        path = route.pattern.pattern.strip("^$")
        path = re.sub(r"\(\?P<\w+>[^)]*\)", "1", path)
        r = httpx.request(route.method, f"{web}{path}", cookies=cookies, headers={**panel_headers(), "Origin": APPS})
        assert not [h for h in r.headers if h.lower().startswith("access-control-")], path


def test_unknown_api_path_and_wrong_method(web):
    assert httpx.get(f"{web}/api/nope").status_code == 404
    assert httpx.put(f"{web}/api/session", headers=panel_headers()).status_code == 405


def test_access_log_has_no_password_or_cookie(web, auth, capfd):
    _login(web, password="wrong password!!")
    httpx.get(f"{web}/api/session?x=1", cookies={"__Host-agento-session": TOKEN})
    err = capfd.readouterr().err
    assert "POST /api/session 401" in err
    assert "wrong password" not in err and TOKEN not in err and "x=1" not in err


ADMIN_MODULE = parse_module_dir(Path(CORE_MODULES_DIR) / "admin")


@pytest.fixture
def config_rows(monkeypatch, tmp_path):
    """`core_config_data` rows at the default scope, as {path: value}; no database."""
    monkeypatch.chdir(tmp_path)
    from agento.framework import scoped_config

    rows: dict[str, str] = {}
    monkeypatch.setattr(scoped_config, "load_scoped_db_overrides",
                        lambda conn, scope, scope_id, **kw: {p: (v, False) for p, v in rows.items()})
    for name in ("CONFIG__ADMIN__LOCALE__DATE_FORMAT", "CONFIG__ADMIN__LOCALE__TIMEZONE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(api, "_DISPLAY", api.display_schema([ADMIN_MODULE]))
    return rows


def _display(web):
    r = httpx.get(f"{web}/api/session", cookies={"__Host-agento-session": TOKEN})
    assert r.status_code == 200
    return r.json()["display"]


def test_session_display_defaults_come_from_the_admin_config_json(web, signed_in, config_rows):
    assert _display(web) == {"date_format": "us", "timezone": "browser", "date_format_explicit": False}


def test_session_display_reads_the_default_scope_db_value(web, signed_in, config_rows):
    config_rows.update({"admin/locale/date_format": "eu", "admin/locale/timezone": "Europe/Warsaw"})
    assert _display(web) == {"date_format": "eu", "timezone": "Europe/Warsaw", "date_format_explicit": True}


def test_session_display_env_wins_over_db(web, signed_in, config_rows, monkeypatch):
    config_rows["admin/locale/date_format"] = "eu"
    monkeypatch.setenv("CONFIG__ADMIN__LOCALE__DATE_FORMAT", "iso")
    display = _display(web)
    assert (display["date_format"], display["date_format_explicit"]) == ("iso", True)


def test_session_display_unknown_value_falls_back_to_the_default(web, signed_in, config_rows):
    config_rows.update({"admin/locale/date_format": "dd/mm", "admin/locale/timezone": "Mars/Olympus"})
    assert _display(web) == {"date_format": "us", "timezone": "browser", "date_format_explicit": False}


def test_disabling_the_admin_module_ends_the_display_at_the_next_session_read(web, signed_in, config_rows,
                                                                              monkeypatch, tmp_path):
    (tmp_path / "app" / "etc").mkdir(parents=True)
    status = tmp_path / "app" / "etc" / "modules.json"
    monkeypatch.chdir(tmp_path)
    shown = []
    for content in ({"admin": True}, {"admin": False}, {"admin": True}):
        status.write_text(json.dumps(content))
        shown.append(_display(web) is not None)

    assert shown == [True, False, True]


def test_session_display_is_null_when_the_admin_module_is_disabled(web, signed_in, config_rows, monkeypatch):
    """No `admin` module in the module set of the start: no display."""
    assert _display(web) is not None
    monkeypatch.setattr(api, "_DISPLAY", api.display_schema([]))
    assert _display(web) is None


@pytest.mark.parametrize("broken", ["system.json", "config.json"])
def test_an_admin_module_whose_display_files_lack_a_field_gives_no_display_and_does_not_stop_the_start(
        tmp_path, broken):
    files = {"system.json": {"locale/date_format": {"type": "select", "options": [{"value": "us", "label": "US"}]},
                             "locale/timezone": {"type": "select", "options_source": "timezones"}},
             "config.json": {"locale/date_format": "us", "locale/timezone": "browser"}}
    whole = parse_module_dir(write_module(tmp_path / "whole", "admin", **files))
    lacking = parse_module_dir(write_module(tmp_path / "lacking", "admin", **{**files, broken: {}}))

    assert set(api.display_schema([whole])) == {"date_format", "timezone"}
    assert api.display_schema([lacking]) is None


def test_login_carries_the_display_too(web, auth, config_rows):
    """The panel boots once; after a login it does not GET the session again."""
    config_rows["admin/locale/date_format"] = "iso"
    assert _login(web).json()["display"] == {"date_format": "iso", "timezone": "browser",
                                             "date_format_explicit": True}
