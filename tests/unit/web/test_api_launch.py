from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest

from agento.framework.access import accounts, launches, sessions
from agento.web import api, rate_limit, toolbox_client

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
    monkeypatch.setattr(launches, "retention_lock", lambda conn, code: contextlib.nullcontext())


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


def _authz(web, version, secret=SECRET, uri=None):
    return httpx.get(f"{web}/internal/authz/app", cookies={f"__Host-agento-launch-{LID}": "tok"},
                     headers={"X-Agento-Proxy-Auth": secret,
                              "X-Forwarded-Uri": uri or f"/a/app/v/{version}/index.html?x=1"})


def test_authz_app_without_the_secret_is_401_even_with_a_launch_cookie(web, monkeypatch, proxy_secret):
    monkeypatch.setattr(launches, "authorize_files", MagicMock(side_effect=AssertionError("must not be called")))
    assert _authz(web, V1, secret="").status_code == 401
    assert _authz(web, V1, secret="x" * 64).status_code == 401


def test_authz_app_allows_only_the_pinned_version(web, monkeypatch, proxy_secret):
    monkeypatch.setattr(launches, "authorize_files",
                        lambda conn, tokens, code, version: tokens == ["tok"] and (code, version) == ("app", V1))
    monkeypatch.setattr(launches, "live_launch_ids", lambda conn, ids: set(ids))
    allowed = _authz(web, V1)
    assert allowed.status_code == 200
    assert allowed.headers["X-Agento-Upstream-Path"] == f"/app/v/{V1}/index.html"
    denied = _authz(web, V2)
    assert denied.status_code == 403
    assert "Set-Cookie" not in denied.headers  # the launch is live: its cookie stays


def test_authz_app_decides_on_the_parsed_path_only(web, monkeypatch, proxy_secret):
    seen = []
    monkeypatch.setattr(launches, "authorize_files", lambda conn, tokens, code, version: seen.append((code, version)) or True)
    traversal = f"/a/app/v/{V1}/../../../other/v/{V2}/index.html"
    r = _authz(web, V1, uri=traversal)
    assert r.status_code == 404 and seen == []  # refused before any launch lookup
    r = httpx.get(f"{web}/internal/authz/app", cookies={f"__Host-agento-launch-{LID}": "tok"},
                  headers={"X-Agento-Proxy-Auth": SECRET, "X-Forwarded-Uri": f"/a/app/v/{V1}/x",
                           "X-Agento-Artifact-Code": "other", "X-Agento-Version-Id": V2})
    assert r.status_code == 200 and seen == [("app", V1)]  # the old headers decide nothing


def test_authz_app_deny_clears_a_dead_launch_cookie(web, monkeypatch, proxy_secret):
    monkeypatch.setattr(launches, "authorize_files", lambda *a: False)
    monkeypatch.setattr(launches, "live_launch_ids", lambda conn, ids: set())
    r = _authz(web, V1)
    assert r.status_code == 403
    assert r.headers["Set-Cookie"].startswith(f"__Host-agento-launch-{LID}=;")


def test_authz_app_never_clears_a_cookie_past_the_bound(web, monkeypatch, proxy_secret):
    ids = [f"{i:032x}" for i in range(21)]
    seen = {}
    monkeypatch.setattr(launches, "authorize_files", lambda conn, tokens, *a: seen.setdefault("tokens", tokens) and False)
    monkeypatch.setattr(launches, "live_launch_ids", lambda conn, lids: seen.setdefault("ids", list(lids)) and set())
    r = httpx.get(f"{web}/internal/authz/app", cookies={f"__Host-agento-launch-{i}": f"t{i}" for i in ids},
                  headers={"X-Agento-Proxy-Auth": SECRET, "X-Forwarded-Uri": f"/a/app/v/{V1}/"})
    assert r.status_code == 403
    assert len(seen["tokens"]) == len(seen["ids"]) == 20
    cleared = {c.split("=")[0] for c in r.headers.get_list("Set-Cookie")}
    assert cleared == {f"__Host-agento-launch-{i}" for i in seen["ids"]}  # the 21st is neither read nor cleared


@pytest.mark.parametrize("path", ["/internal/authz/app", "/internal/authz/share"])
def test_rotating_launch_cookies_are_counted_on_the_shared_bucket(web, monkeypatch,
                                                                  proxy_secret, path):
    """SEC-12. The proxy's secret proves the HOP, not the caller.

    Read as a caller identity it was a way through the limiter on the only routes the proxy
    can reach: every rotated cookie is a fresh PRIVATE bucket, and the shared bucket - the
    one that is supposed to hold a brute force - was neither counted nor told. Each rejected
    attempt must reach it, whichever of the two authorization paths answered.
    """
    monkeypatch.setattr(launches, "authorize_files", lambda *a: False)
    monkeypatch.setattr(launches, "live_launch_ids", lambda conn, ids: set(ids))
    failures: list[list] = []
    monkeypatch.setattr(rate_limit, "record_auth_failure",
                        lambda conn, buckets, **kw: failures.append(buckets))

    for i in range(3):
        r = httpx.get(f"{web}{path}", cookies={f"__Host-agento-launch-{i:032x}": f"t{i}"},
                      headers={"X-Agento-Proxy-Auth": SECRET,
                               "X-Agento-Artifact-Code": "app", "X-Agento-Version-Id": V1})
        assert r.status_code == 403

    assert len(failures) == 3
    assert all(any(not b.private for b in buckets) for buckets in failures)


def test_a_held_address_cannot_keep_guessing_through_the_proxy(web, monkeypatch, proxy_secret):
    """The hold those failures place has to reach the same route - without the subrequest
    itself spending the shared ceiling, since the proxy makes one per request it forwards."""
    monkeypatch.setattr(launches, "authorize_files",
                        MagicMock(side_effect=AssertionError("held: must not be reached")))
    counted: list = []
    monkeypatch.setattr(rate_limit, "check",
                        lambda conn, buckets, **kw: counted.append(buckets)
                        or rate_limit.Decision(True))
    monkeypatch.setattr(rate_limit, "held", lambda conn, buckets: 42)

    r = _authz(web, V1)

    assert r.status_code == 429
    assert r.headers["Retry-After"] == "42"
    assert all(all(b.private for b in buckets) for buckets in counted)  # nothing shared spent


def test_the_proxy_asking_for_a_visitor_with_no_cookie_is_not_a_failed_guess(web, monkeypatch,
                                                                            proxy_secret):
    """One subrequest per forwarded request: an ordinary visitor with no launch cookie would
    otherwise place the hold that exists to stop guessing. Nothing presented, nothing guessed."""
    monkeypatch.setattr(rate_limit, "record_auth_failure",
                        MagicMock(side_effect=AssertionError("must not be counted")))
    r = httpx.get(f"{web}/internal/authz/app", headers={"X-Agento-Proxy-Auth": SECRET})
    assert r.status_code == 403


def test_create_launch_holds_the_retention_lock_around_current_and_the_insert(web, monkeypatch, signed_in):
    events = []

    @contextlib.contextmanager
    def lock(conn, code):
        events.append(("lock", code))
        yield
        events.append(("unlock", code))

    monkeypatch.setattr(launches, "retention_lock", lock)
    invoke = _current(monkeypatch)
    invoke.side_effect = lambda *a, **k: events.append("current") or toolbox_client.InvokeResult(
        200, {"ok": True, "result": {"content": [{"type": "text", "text": json.dumps({"current_version": V1})}]}})
    monkeypatch.setattr(launches, "create_launch", lambda *a, **k: events.append("insert") or (_launch(), "c"))
    assert _post_launch(web, {"agent_view_id": 3, "artifact_code": "app"}).status_code == 201
    assert events == [("lock", "app"), "current", "insert", ("unlock", "app")]


def test_create_launch_is_503_when_the_retention_lock_is_busy(web, monkeypatch, signed_in):
    def busy(conn, code):
        raise launches.RetentionBusy(code)

    monkeypatch.setattr(launches, "retention_lock", busy)
    invoke = _current(monkeypatch)
    assert _post_launch(web, {"agent_view_id": 3, "artifact_code": "app"}).status_code == 503
    invoke.assert_not_called()
