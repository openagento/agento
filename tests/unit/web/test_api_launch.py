from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest

from agento.framework.access import accounts, launches, sessions
from agento.web import api, toolbox_client

from .conftest import APPS, PANEL, panel_headers

TOKEN = "session-token-value"
USER = accounts.User(id=2, username="alice", role="user", is_active=True)
V1, V2 = "v-20260101-000000-aaaa", "v-20260102-000000-bbbb"
LID, LID2 = "a" * 32, "b" * 32
SECRET = "s" * 64
LATER = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)


def _launch(launch_id=LID, version=V1):
    return launches.Launch(id=launch_id, user_id=USER.id, artifact_code="app", version_id=version,
                           workspace_id=1, agent_view_id=3, expires_at=LATER)


@pytest.fixture
def signed_in(monkeypatch):
    session = sessions.Session(id="sid", user=USER, expires_at=LATER)
    monkeypatch.setattr(sessions, "lookup_session", lambda conn, t: session if t == TOKEN else None)
    monkeypatch.setattr(api, "_resolve_scope", lambda req, body: (1, body["agent_view_id"]))
    monkeypatch.setattr(accounts, "has_operation", lambda *a: True)


def _post_launch(web, body):
    headers = panel_headers(**{"X-CSRF-Token": sessions.csrf_token(TOKEN)})
    return httpx.post(f"{web}/api/launches", json=body, cookies={"__Host-agento-session": TOKEN}, headers=headers)


def _current(monkeypatch, status=200, body=None):
    if body is None:
        text = json.dumps({"artifact_code": "app", "current_version": V1})
        body = {"ok": True, "result": {"content": [{"type": "text", "text": text}]}}
    invoke = MagicMock(return_value=toolbox_client.InvokeResult(status, body))
    monkeypatch.setattr(toolbox_client, "invoke_tool", invoke)
    return invoke


def test_create_launch_resolves_current_and_returns_a_form_not_a_url(web, monkeypatch, signed_in):
    invoke = _current(monkeypatch)
    create = MagicMock(return_value=(_launch(), "the-code"))
    monkeypatch.setattr(launches, "create_launch", create)
    r = _post_launch(web, {"agent_view_id": 3, "artifact_code": "app"})
    assert r.status_code == 201
    assert invoke.call_args.args[2:] == ("versioned_artifact_get_current", {"artifact_code": "app"})
    assert create.call_args.kwargs == {"workspace_id": 1, "agent_view_id": 3, "artifact_code": "app", "version_id": V1}
    body = r.json()
    assert body["redeem"] == {"url": f"{APPS}/launch", "fields": {"launch_id": LID, "code": "the-code"}}
    assert "the-code" not in body["redeem"]["url"]


@pytest.mark.parametrize(("body", "status"), [
    ({"artifact_code": "app"}, 400),
    ({"agent_view_id": 3, "artifact_code": "App!"}, 400),
    ({"agent_view_id": 3, "workspace_id": 1, "artifact_code": "app"}, 400),
])
def test_create_launch_input_errors(web, monkeypatch, signed_in, body, status):
    _current(monkeypatch)
    assert _post_launch(web, body).status_code == status


def test_create_launch_without_the_operation_is_404(web, monkeypatch, signed_in):
    monkeypatch.setattr(accounts, "has_operation", lambda *a: False)
    invoke = _current(monkeypatch)
    assert _post_launch(web, {"agent_view_id": 3, "artifact_code": "app"}).status_code == 404
    invoke.assert_not_called()


@pytest.mark.parametrize(("status", "body", "expected"), [
    (200, {"ok": True, "result": {"content": [{"type": "text", "text": "{\"current_version\": null}"}]}}, 409),
    (404, {"ok": False, "error": {"code": "not_found"}}, 404),
    (503, {"ok": False, "error": {"code": "toolbox_unavailable"}}, 503),
    (200, {"ok": True, "result": "garbage"}, 503),
])
def test_create_launch_current_failures(web, monkeypatch, signed_in, status, body, expected):
    _current(monkeypatch, status, body)
    monkeypatch.setattr(launches, "create_launch", MagicMock(side_effect=AssertionError("must not be called")))
    assert _post_launch(web, {"agent_view_id": 3, "artifact_code": "app"}).status_code == expected


@pytest.mark.parametrize(("exc", "expected"), [
    (launches.AccessConfigError("bad"), 503), (accounts.AccessError("not allowed"), 404)])
def test_create_launch_service_refusals(web, monkeypatch, signed_in, exc, expected):
    _current(monkeypatch)
    monkeypatch.setattr(launches, "create_launch", MagicMock(side_effect=exc))
    assert _post_launch(web, {"agent_view_id": 3, "artifact_code": "app"}).status_code == expected


def test_end_someone_elses_launch_is_404(web, monkeypatch, signed_in):
    monkeypatch.setattr(launches, "revoke_launch", lambda conn, user, lid: False)
    headers = panel_headers(**{"X-CSRF-Token": sessions.csrf_token(TOKEN)})
    r = httpx.delete(f"{web}/api/launches/{LID}", cookies={"__Host-agento-session": TOKEN}, headers=headers)
    assert r.status_code == 404


def _redeem(web, launch_id=LID, code="the-code", cookies=None, **headers):
    base = {"Origin": PANEL, "Sec-Fetch-Site": "same-site", "Content-Type": "application/x-www-form-urlencoded"}
    return httpx.post(f"{web}/internal/launch/redeem", content=f"launch_id={launch_id}&code={code}",
                      headers={**base, **headers}, cookies=cookies or {})


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT"])
def test_redeem_answers_405_to_anything_but_post(web, monkeypatch, method):
    monkeypatch.setattr(launches, "redeem", MagicMock(side_effect=AssertionError("must not be called")))
    assert httpx.request(method, f"{web}/internal/launch/redeem?launch_id={LID}&code=x").status_code == 405


def test_redeem_sets_the_per_launch_cookie_and_clears_dead_ones(web, monkeypatch):
    monkeypatch.setattr(launches, "redeem", lambda conn, lid, code: (_launch(lid), "launch-token"))
    monkeypatch.setattr(launches, "live_launch_ids", lambda conn, ids: set())
    r = _redeem(web, cookies={f"__Host-agento-launch-{LID2}": "old"})
    assert r.status_code == 303
    assert r.headers["Location"] == f"/a/app/v/{V1}/"
    set_cookies = r.headers.get_list("Set-Cookie")
    mine = next(c for c in set_cookies if c.startswith(f"__Host-agento-launch-{LID}=launch-token"))
    for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/"):
        assert flag in mine
    assert "Domain" not in mine
    assert any(c.startswith(f"__Host-agento-launch-{LID2}=;") and "Max-Age=0" in c for c in set_cookies)


def test_two_launches_get_two_cookie_names(web, monkeypatch):
    monkeypatch.setattr(launches, "redeem", lambda conn, lid, code: (_launch(lid), "t-" + lid[0]))
    monkeypatch.setattr(launches, "live_launch_ids", lambda conn, ids: set(ids))
    names = {_redeem(web, lid).headers["Set-Cookie"].split("=")[0] for lid in (LID, LID2)}
    assert names == {f"__Host-agento-launch-{LID}", f"__Host-agento-launch-{LID2}"}


@pytest.mark.parametrize("headers", [
    {"Origin": APPS}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"},
])
def test_redeem_requires_the_panel_page(web, monkeypatch, headers):
    monkeypatch.setattr(launches, "redeem", MagicMock(side_effect=AssertionError("must not be called")))
    assert _redeem(web, **headers).status_code == 403


def test_redeem_loss_sets_no_cookie(web, monkeypatch):
    monkeypatch.setattr(launches, "redeem", lambda conn, lid, code: None)
    r = _redeem(web)
    assert r.status_code == 403
    assert "Set-Cookie" not in r.headers


def test_redeem_rejects_a_bad_id_and_a_json_body(web, monkeypatch):
    monkeypatch.setattr(launches, "redeem", MagicMock(side_effect=AssertionError("must not be called")))
    assert _redeem(web, launch_id="../x").status_code == 403
    assert _redeem(web, **{"Content-Type": "application/json"}).status_code == 400


@pytest.fixture
def proxy_secret(tmp_path):
    (tmp_path / "proxy-secret").write_text(SECRET)


def _authz(web, version, secret=SECRET):
    return httpx.get(f"{web}/internal/authz/app", cookies={f"__Host-agento-launch-{LID}": "tok"},
                     headers={"X-Agento-Proxy-Auth": secret, "X-Agento-Artifact-Code": "app",
                              "X-Agento-Version-Id": version})


def test_authz_app_without_the_secret_is_401_even_with_a_launch_cookie(web, monkeypatch, proxy_secret):
    monkeypatch.setattr(launches, "authorize_files", MagicMock(side_effect=AssertionError("must not be called")))
    assert _authz(web, V1, secret="").status_code == 401
    assert _authz(web, V1, secret="x" * 64).status_code == 401


def test_authz_app_allows_only_the_pinned_version(web, monkeypatch, proxy_secret):
    monkeypatch.setattr(launches, "authorize_files",
                        lambda conn, tokens, code, version: tokens == ["tok"] and (code, version) == ("app", V1))
    monkeypatch.setattr(launches, "live_launch_ids", lambda conn, ids: set(ids))
    assert _authz(web, V1).status_code == 200
    denied = _authz(web, V2)
    assert denied.status_code == 403
    assert "Set-Cookie" not in denied.headers  # the launch is live: its cookie stays


def test_authz_app_deny_clears_a_dead_launch_cookie(web, monkeypatch, proxy_secret):
    monkeypatch.setattr(launches, "authorize_files", lambda *a: False)
    monkeypatch.setattr(launches, "live_launch_ids", lambda conn, ids: set())
    r = _authz(web, V1)
    assert r.status_code == 403
    assert r.headers["Set-Cookie"].startswith(f"__Host-agento-launch-{LID}=;")
