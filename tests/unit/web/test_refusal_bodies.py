"""What a refusal looks like on the wire: the text and status as before, plus a code (AC7)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from agento.framework import config_write
from agento.framework.access import accounts, sessions
from agento.web import api, rate_limit, server

from .conftest import TOKEN, a_session, panel_headers, sign_in_as

COOKIES = {"__Host-agento-session": TOKEN}
ADMIN = accounts.User(id=1, username="root", role="admin", is_active=True)
VERSION = "v-20260101-000000-aaaa"


def call(web, method, path, body=None):
    headers = panel_headers(**{"X-CSRF-Token": sessions.csrf_token(TOKEN)})
    return httpx.request(method, f"{web}{path}", cookies=COOKIES, headers=headers,
                         **({"json": body} if body is not None else {}))


@pytest.mark.parametrize(("code", "params", "body"), [
    (None, None, {"error": "x"}),
    ("NOT_FOUND", None, {"error": "x", "code": "NOT_FOUND"}),
    ("ROLE_IN_USE", {"count": 2}, {"error": "x", "code": "ROLE_IN_USE", "params": {"count": 2}}),
])
def test_error_sends_code_and_params_only_when_given(code, params, body):
    assert api.error(400, "x", code=code, params=params).body == body


def test_login_refusals_keep_text_and_status_and_carry_a_code(web, monkeypatch):
    monkeypatch.setattr(sessions, "sign_in", lambda conn, username, password: None)

    def login(body):
        r = httpx.post(f"{web}/api/session", json=body, headers=panel_headers())
        return r.status_code, r.json()

    wrong = {"username": "alice", "password": "wrong password!!"}
    assert login({"username": "alice"}) == (
        400, {"error": "username and password are required", "code": "INVALID_REQUEST"})
    for _ in range(api.THROTTLE.LIMIT):
        assert login(wrong) == (401, {"error": "invalid credentials", "code": "AUTH_INVALID_CREDENTIALS"})
    assert login(wrong) == (429, {"error": "too many failed logins, try later", "code": "AUTH_THROTTLED"})


def test_a_rate_limited_request_keeps_retry_after_and_carries_a_code(web, monkeypatch):
    monkeypatch.setattr(rate_limit, "check", lambda conn, buckets, *, cfg=None: rate_limit.Decision(False, 7))

    r = httpx.get(f"{web}/api/session", cookies=COOKIES)

    assert (r.status_code, r.headers["retry-after"]) == (429, "7")
    assert r.json() == {"error": "too many requests", "code": "RATE_LIMITED"}


def _no_counter(*_a, **_kw):
    raise RuntimeError("the counter is down")


def _no_shared_counter(conn, buckets, *, cfg=None):
    if any(not bucket.private for bucket in buckets):
        raise RuntimeError("the counter is down")
    return rate_limit.Decision(True)


@pytest.mark.parametrize(("counter", "broken"), [
    pytest.param("check", _no_counter, id="the-private-buckets"),
    pytest.param("check", _no_shared_counter, id="the-shared-buckets"),
    pytest.param("record_auth_failure", _no_counter, id="a-failed-authentication"),
])
def test_a_limiter_that_cannot_count_refuses_through_the_serializer(web, monkeypatch, counter, broken):
    monkeypatch.setattr(rate_limit, counter, broken)

    r = httpx.get(f"{web}/api/session")

    assert (r.status_code, r.headers["content-type"]) == (503, "application/json")
    assert r.json() == {"error": "service unavailable", "code": "SERVICE_UNAVAILABLE"}
    assert r.headers.get_list("cache-control") == ["no-store"]


@pytest.mark.parametrize(("method", "path", "status", "body"), [
    ("GET", "/api/nope", 404, {"error": "not found", "code": "NOT_FOUND"}),
    ("PUT", "/api/session", 405, {"error": "method not allowed", "code": "METHOD_NOT_ALLOWED"}),
    ("OPTIONS", "/api/session", 405, {"error": "method not allowed", "code": "METHOD_NOT_ALLOWED"}),
])
def test_transport_refusals_carry_a_code(web, method, path, status, body):
    r = httpx.request(method, f"{web}{path}", headers=panel_headers())

    assert (r.status_code, r.json()) == (status, body)


def test_a_handler_that_fails_answers_internal_error_with_a_code(web, monkeypatch):
    sign_in_as(monkeypatch, ADMIN)
    monkeypatch.setattr(accounts, "held_admin", MagicMock(side_effect=RuntimeError("boom")))

    r = httpx.get(f"{web}/api/session", cookies=COOKIES)

    assert (r.status_code, r.json()) == (500, {"error": "internal error", "code": "INTERNAL_ERROR"})


@pytest.mark.parametrize(("refused", "status", "body"), [
    (config_write.ConfigWriteError("set this field with bin/agento config:set", code="CONFIG_USE_CLI"), 400,
     {"error": "set this field with bin/agento config:set", "code": "CONFIG_USE_CLI"}),
    (config_write.ConfigWriteError("Error: Invalid config path 'x'.", code="CONFIG_PATH_INVALID",
                                   params={"path": "x"}), 400,
     {"error": "Error: Invalid config path 'x'.", "code": "CONFIG_PATH_INVALID", "params": {"path": "x"}}),
    (config_write.ConfigWriteError("not allowed", code="FORBIDDEN"), 403, {"error": "forbidden", "code": "FORBIDDEN"}),
    (config_write.ConfigWriteError("a module that passes no code"), 400, {"error": "a module that passes no code"}),
])
def test_config_refusals_keep_text_and_status_and_carry_the_code(web, monkeypatch, refused, status, body):
    sign_in_as(monkeypatch, ADMIN)
    monkeypatch.setattr(config_write, "save_config", MagicMock(side_effect=refused))

    r = call(web, "PUT", "/api/admin/config", {"path": "x", "value": "1"})

    assert (r.status_code, r.json()) == (status, body)


def test_the_real_config_validation_raises_with_a_code():
    with pytest.raises(config_write.ConfigWriteError) as refused:
        config_write.validate_config_path("no-slash")

    assert (refused.value.code, refused.value.params) == ("CONFIG_PATH_INVALID", {"path": "no-slash"})
    assert str(refused.value) == "Error: Invalid config path 'no-slash'."


def test_a_config_write_by_a_demoted_actor_keeps_the_access_code(monkeypatch):
    conn = MagicMock()
    monkeypatch.setattr(accounts, "_lock_actor_and",
                        MagicMock(side_effect=accounts.AccessError("not allowed", code="FORBIDDEN")))

    with pytest.raises(config_write.ConfigWriteError) as refused:
        config_write.write_config(conn, "a/b", "1", scope="default", scope_id=0, actor=(1, "admin"))

    assert (str(refused.value), refused.value.code) == ("not allowed", "FORBIDDEN")


@pytest.mark.parametrize(("refused", "status", "body"), [
    (accounts.AccessError("not allowed", code="FORBIDDEN"), 403, {"error": "forbidden", "code": "FORBIDDEN"}),
    (accounts.AccessError("user not found", code="USER_NOT_FOUND"), 404,
     {"error": "user not found", "code": "USER_NOT_FOUND"}),
    (accounts.AccessError("2 users have this role", code="ROLE_IN_USE", params={"count": 2}), 400,
     {"error": "2 users have this role", "code": "ROLE_IN_USE", "params": {"count": 2}}),
])
def test_account_refusals_keep_text_and_status_and_carry_the_code(web, monkeypatch, refused, status, body):
    sign_in_as(monkeypatch, ADMIN)
    monkeypatch.setattr(accounts, "update_user", MagicMock(side_effect=refused))

    r = call(web, "PATCH", "/api/admin/users/7", {"is_active": False})

    assert (r.status_code, r.json()) == (status, body)


def test_a_miniapp_refusal_keeps_its_module_field_and_carries_a_code():
    from agento.modules.miniapps.src import activation, routes

    def approve():
        raise activation.ActivationError(409, "current version changed, reload", code="CURRENT_VERSION_CHANGED",
                                         current_version="v3")

    resp = routes._run(approve)

    assert (resp.status, resp.body) == (409, {"error": "current version changed, reload",
                                              "code": "CURRENT_VERSION_CHANGED", "current_version": "v3"})


@pytest.mark.parametrize(("store_code", "code"), [
    ("CURRENT_VERSION_CHANGED", "CURRENT_VERSION_CHANGED"),
    (None, "ARTIFACT_REFUSED"),
    ("not a code", "ARTIFACT_REFUSED"),
])
def test_an_artifact_refusal_keeps_its_module_field_and_carries_the_store_code(monkeypatch, store_code, code):
    from agento.modules.versioned_artifacts.src import admin, routes
    from agento.web import toolbox_client

    answer = SimpleNamespace(status=409, body={"message": "current moved", "error_code": store_code,
                                               "current_version": "v3"})
    monkeypatch.setattr(toolbox_client, "admin_request", lambda *a, **kw: answer)
    req = api.Request(method="POST", path="/", headers={}, body=b"", cookies={}, origins=None,
                      conn=MagicMock(), session=a_session(ADMIN))

    resp = routes._run(req, lambda user: admin._store(req.conn, user, "POST", "/x/publish"))

    assert (resp.status, resp.body) == (409, {"error": "current moved", "code": code, "current_version": "v3"})


def test_a_stale_launch_cookie_refusal_keeps_its_cookie_clear_and_carries_a_code(monkeypatch):
    from agento.framework.access import launches
    from agento.web import security

    launch_id = "a" * 32
    monkeypatch.setattr(launches, "authorize_files", lambda *a: False)
    monkeypatch.setattr(launches, "live_launch_ids", lambda conn, ids: set())
    req = api.Request(method="GET", path="/", headers={"X-Forwarded-Uri": f"/a/demo/v/{VERSION}/index.html"}, body=b"",
                      cookies={security.launch_cookie_name(launch_id): "t" * 43}, origins=None, conn=MagicMock())

    resp = api.authorize_app(req)

    assert (resp.status, resp.body) == (403, {"error": "forbidden", "code": "FORBIDDEN"})
    assert [name for name, _ in resp.headers] == ["Set-Cookie"]


def test_the_rate_limit_refusal_is_built_by_the_one_serializer():
    resp = server._too_many(3)

    assert (resp.status, resp.body, resp.headers) == (
        429, {"error": "too many requests", "code": "RATE_LIMITED"}, [("Retry-After", "3")])
