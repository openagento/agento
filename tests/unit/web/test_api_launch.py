from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest

from agento.framework.access import accounts, launches, sessions
from agento.web import rate_limit, toolbox_client

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


def test_end_someone_elses_launch_is_404(web, monkeypatch, signed_in):
    monkeypatch.setattr(launches, "revoke_launch", lambda conn, user, lid: False)
    headers = panel_headers(**{"X-CSRF-Token": sessions.csrf_token(TOKEN)})
    r = httpx.delete(f"{web}/api/launches/{LID}", cookies={"__Host-agento-session": TOKEN}, headers=headers)
    assert r.status_code == 404


def _redeem(web, launch_id=LID, code="the-code", cookies=None, **headers):
    base = {"Origin": PANEL, "Sec-Fetch-Site": "same-site", "Content-Type": "application/x-www-form-urlencoded"}
    sent = {k: v for k, v in {**base, **headers}.items() if v is not None}
    return httpx.post(f"{web}/internal/launch/redeem", content=f"launch_id={launch_id}&code={code}",
                      headers=sent, cookies=cookies or {})


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


@pytest.mark.parametrize("site", ["same-site", "cross-site", None])
def test_redeem_accepts_the_panel_origin_whatever_the_site(web, monkeypatch, site):
    """panel.localhost and apps.localhost are two sites to a browser: the exact Origin is the check."""
    monkeypatch.setattr(launches, "redeem", lambda conn, lid, code: (_launch(lid), "launch-token"))
    monkeypatch.setattr(launches, "live_launch_ids", lambda conn, ids: set())
    assert _redeem(web, **{"Sec-Fetch-Site": site}).status_code == 303


@pytest.mark.parametrize("headers", [
    {"Origin": APPS}, {"Origin": "https://evil.example"}, {"Origin": None},
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


def test_rotating_launch_cookies_are_counted_on_the_shared_bucket(web, monkeypatch, proxy_secret):
    """SEC-12. The proxy's secret proves the HOP, not the caller.

    Read as a caller identity it was a way through the limiter on the only routes the proxy
    can reach: every rotated cookie is a fresh PRIVATE bucket, and the shared bucket - the
    one that is supposed to hold a brute force - was neither counted nor told. Each rejected
    attempt must reach it.
    """
    monkeypatch.setattr(launches, "authorize_files", lambda *a: False)
    monkeypatch.setattr(launches, "live_launch_ids", lambda conn, ids: set(ids))
    failures: list[list] = []
    monkeypatch.setattr(rate_limit, "record_auth_failure",
                        lambda conn, buckets, **kw: failures.append(buckets))

    for i in range(3):
        r = httpx.get(f"{web}/internal/authz/app", cookies={f"__Host-agento-launch-{i:032x}": f"t{i}"},
                      headers={"X-Agento-Proxy-Auth": SECRET, "X-Forwarded-Uri": f"/a/app/v/{V1}/index.html"})
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
    r = httpx.get(f"{web}/internal/authz/app",
                  headers={"X-Agento-Proxy-Auth": SECRET, "X-Forwarded-Uri": f"/a/app/v/{V1}/index.html"})
    assert r.status_code == 403


def _post_action(web, tool="notes_add", body=None):
    headers = panel_headers(**{"X-CSRF-Token": sessions.csrf_token(TOKEN)})
    return httpx.post(f"{web}/api/launches/{LID}/actions/{tool}", json={"arguments": {"x": 1}} if body is None else body,
                      cookies={"__Host-agento-session": TOKEN}, headers=headers)


def test_launch_action_passes_the_launch_and_the_tool_through(web, monkeypatch, signed_in):
    call = MagicMock(return_value=toolbox_client.InvokeResult(200, {"ok": True, "result": {}}))
    monkeypatch.setattr(toolbox_client, "invoke_launch_action", call)
    r = _post_action(web)
    assert (r.status_code, r.json()) == (200, {"ok": True, "result": {}})
    assert call.call_args.args[2:] == (LID, "notes_add", {"x": 1})


def test_launch_action_refuses_non_object_arguments(web, monkeypatch, signed_in):
    monkeypatch.setattr(toolbox_client, "invoke_launch_action", MagicMock(side_effect=AssertionError("no call")))
    assert _post_action(web, body={"arguments": [1]}).status_code == 400


def test_launch_action_route_rejects_a_bad_tool_name(web, monkeypatch, signed_in):
    monkeypatch.setattr(toolbox_client, "invoke_launch_action", MagicMock(side_effect=AssertionError("no call")))
    assert _post_action(web, tool="Bad-Name").status_code == 404


def _miniapps_disabled(monkeypatch):
    from agento.framework import module_status
    monkeypatch.setattr(module_status, "read_module_status", lambda path=None: {"miniapps": False})


def test_a_disabled_miniapps_module_refuses_actions(web, monkeypatch, signed_in):
    """MOD-1: web is the only minter of a miniapp capability, so it stops here."""
    _miniapps_disabled(monkeypatch)
    monkeypatch.setattr(toolbox_client, "invoke_launch_action", MagicMock(side_effect=AssertionError("no call")))
    assert _post_action(web).status_code == 404
