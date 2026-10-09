"""Health, and the authorization endpoints only the proxy can reach.

`toolbox` shares db-net with `web`, so a direct call from it looks exactly like a
request with no (or a forged) X-Agento-Proxy-Auth header — the toolbox cannot read the
secret, because only `proxy` and `web` mount its volume (asserted in test_provisioning).
"""
from __future__ import annotations

import re

import httpx
import pytest

SECRET = "a" * 64
APP = "/a/demo/v/v-20260925-120000-ab12/index.html"


@pytest.fixture
def secret_file(tmp_path, monkeypatch):
    path = tmp_path / "proxy-secret"
    monkeypatch.setenv("AGENTO_PROXY_SECRET_FILE", str(path))
    return path


@pytest.fixture
def base_url(secret_file, live):
    return live


def test_health_answers_ok(base_url):
    r = httpx.get(f"{base_url}/health")
    assert r.status_code == 200
    assert r.text == "ok"


def test_direct_call_without_the_proxy_secret_is_denied(base_url, secret_file):
    secret_file.write_text(SECRET)
    assert httpx.get(f"{base_url}/internal/authz/app").status_code == 401


def test_there_is_no_share_authz_endpoint(base_url, secret_file):
    # Shares are checked by the artifacts server (DECISIONS 2026-09-27, E6 S1).
    secret_file.write_text(SECRET)
    assert httpx.get(f"{base_url}/internal/authz/share", headers={"X-Agento-Proxy-Auth": SECRET}).status_code == 404


def test_forged_proxy_secret_is_denied(base_url, secret_file):
    secret_file.write_text(SECRET)
    r = httpx.get(f"{base_url}/internal/authz/app", headers={"X-Agento-Proxy-Auth": "b" * 64})
    assert r.status_code == 401


def test_caller_identity_headers_without_the_secret_are_denied(base_url, secret_file):
    secret_file.write_text(SECRET)
    r = httpx.get(f"{base_url}/internal/authz/app",
                  headers={"X-Agento-User": "1", "X-Forwarded-User": "admin"})
    assert r.status_code == 401


def test_the_proxy_gets_a_deny_without_a_launch_cookie(base_url, secret_file):
    secret_file.write_text(SECRET)
    r = httpx.get(f"{base_url}/internal/authz/app", headers={"X-Agento-Proxy-Auth": SECRET, "X-Forwarded-Uri": APP})
    assert r.status_code == 403


@pytest.mark.parametrize("uri", [None, "/a/demo/", "/a/demo/current/index.html",
                                 "/a/demo/v/v-20260925-120000-ab12/%2e%2e/other/index.html"])
def test_a_path_that_is_not_a_version_path_is_404_without_a_launch_cookie(base_url, secret_file, uri):
    # The proxy serves only /a/<code>/v/<id>/ (PRD E6 §6.2): the path decides before the cookie.
    secret_file.write_text(SECRET)
    headers = {"X-Agento-Proxy-Auth": SECRET} | ({"X-Forwarded-Uri": uri} if uri else {})
    assert httpx.get(f"{base_url}/internal/authz/app", headers=headers).status_code == 404


def test_missing_secret_on_web_fails_closed(base_url, secret_file):
    r = httpx.get(f"{base_url}/internal/authz/app", headers={"X-Agento-Proxy-Auth": ""})
    assert r.status_code == 401


def test_secret_written_after_start_is_picked_up(base_url, secret_file):
    headers = {"X-Agento-Proxy-Auth": SECRET, "X-Forwarded-Uri": APP}
    assert httpx.get(f"{base_url}/internal/authz/app", headers=headers).status_code == 401
    secret_file.write_text(SECRET + "\n")
    assert httpx.get(f"{base_url}/internal/authz/app", headers=headers).status_code == 403


def test_unknown_path_is_404(base_url):
    assert httpx.get(f"{base_url}/login").status_code == 404


def test_query_string_never_reaches_the_log(base_url, capfd):
    httpx.get(f"{base_url}/internal/authz/app?cap=SECRETCAP&code=SECRETCODE")
    httpx.get(f"{base_url}/health?cap=SECRETCAP")
    err = capfd.readouterr().err
    assert "/internal/authz/app" in err
    assert "SECRET" not in err


def test_a_request_to_an_unknown_path_is_still_counted(base_url, counted):
    """The limiter is mounted before dispatch, so a route cannot escape it by not existing."""
    assert httpx.get(f"{base_url}/nope").status_code == 404
    assert [b.kind for b in counted[-1]] == ["address"]


def test_health_is_not_counted(base_url, counted):
    assert httpx.get(f"{base_url}/health").status_code == 200
    assert counted == []


def test_the_limiter_fails_closed_when_it_cannot_count(base_url, monkeypatch):
    from agento.web import rate_limit

    monkeypatch.setattr(rate_limit, "check", _raise)
    assert httpx.get(f"{base_url}/api/session").status_code == 503


def _raise(*args, **kwargs):
    raise RuntimeError("no database")


def test_every_401_is_recorded_as_a_failed_authentication(base_url, monkeypatch, secret_file):
    """A failed login, a probe without the proxy secret and a dead session all feed the hold."""
    from agento.web import rate_limit

    failures: list = []
    monkeypatch.setattr(rate_limit, "record_auth_failure", lambda conn, buckets, **kw: failures.append(buckets))
    secret_file.write_text(SECRET)

    httpx.get(f"{base_url}/internal/authz/app")          # no proxy secret -> 401
    assert len(failures) == 1
    assert [b.kind for b in failures[0]] == ["address"]


# --- a shared hold is not bypassed by presenting a credential (SEC-12) ------

@pytest.fixture
def held(monkeypatch):
    """The limiter reports a hold on a SHARED bucket, as it does after failed logins."""
    from agento.web import rate_limit
    from agento.web import server as server_module

    monkeypatch.setattr(rate_limit, "check",
                        lambda conn, buckets, *, cfg=None: rate_limit.Decision(True, 900, 900))
    monkeypatch.setattr(server_module, "connect", lambda: None)


@pytest.mark.parametrize("cookie", [None, "__Host-agento-session=not-a-session",
                                    "__Host-agento-launch-" + "d" * 32 + "=not-a-launch"])
def test_a_held_address_is_refused_whatever_cookie_it_presents(base_url, held, cookie):
    """Presenting a credential is not proving one.

    The hold exists because this address just failed to authenticate repeatedly, which is
    what a brute force looks like; if a cookie the server never validated counted as
    authentication, every hold would have a one-header bypass.
    """
    headers = {"Cookie": cookie} if cookie else {}
    response = httpx.get(f"{base_url}/api/does-not-exist", headers=headers)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "900"


def test_a_held_address_still_reaches_health(base_url, held):
    """The one exemption stays exempt: a held address must not make the container unhealthy."""
    assert httpx.get(f"{base_url}/health").status_code == 200


# --- a shared refusal costs the authorized caller nothing, and the stranger no side
#     effect (SEC-12) --------------------------------------------------------

def _a_session():
    from datetime import UTC, datetime, timedelta

    from agento.framework.access import accounts, sessions

    return sessions.Session(
        id="sid", user=accounts.User(id=1, username="alice", role="user", is_active=True),
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))


def test_an_authenticated_caller_survives_another_callers_shared_address_refusal(
        base_url, held, monkeypatch):
    """SEC-12: "the address limit counts failures only, so one caller cannot throttle the
    others' authorized traffic". Behind one NAT, the brute force and the colleague share an
    address; refusing the colleague is the attacker throttling them."""
    from agento.framework.access import sessions
    from agento.web import security
    from agento.web import server as server_module

    monkeypatch.setattr(server_module, "connect", lambda: None)
    monkeypatch.setattr(sessions, "lookup_session", lambda conn, t: _a_session())

    response = httpx.get(f"{base_url}/api/session", headers={"Cookie": f"{security.SESSION_COOKIE}=real"})

    assert response.status_code == 200


def test_a_shared_refusal_lands_before_the_password_is_derived(base_url, held, monkeypatch):
    """The refusal is a decision about whether the handler runs, not about which body is
    written afterwards. A 429 that replaces the answer has already paid for the work."""
    from agento.framework.access import sessions

    tried: list = []
    monkeypatch.setattr(sessions, "sign_in", lambda conn, u, p: tried.append(u))

    response = httpx.post(f"{base_url}/api/session", json={"username": "alice", "password": "x"},
                          headers={"Origin": "https://panel.localhost:8443",
                                   "Sec-Fetch-Site": "same-origin"})

    assert response.status_code == 429
    assert tried == []


def test_a_shared_refusal_consumes_no_launch_code(base_url, held, monkeypatch):
    """Redemption spends a single-use code and commits. A refusal that arrives after it has
    burned the caller's only code and handed them a 429 for it."""
    from agento.web import api

    redeemed: list = []
    monkeypatch.setattr(api, "redeem_launch", lambda req: redeemed.append(req.body))

    response = httpx.post(f"{base_url}/internal/launch/redeem", data={"code": "one-use"})

    assert response.status_code == 429
    assert redeemed == []


def test_an_invalid_launch_redemption_is_counted_as_a_failed_authentication(
        base_url, monkeypatch):
    """A spent or forged exchange code is answered 403, not 401 - and a flood of them is a
    brute force whichever status the route reached for (SEC-12 names both)."""
    from agento.web import api, rate_limit

    failures: list = []
    monkeypatch.setattr(rate_limit, "record_auth_failure",
                        lambda conn, buckets, **kw: failures.append(buckets))
    monkeypatch.setattr(api, "redeem_launch", lambda req: api.error(403, "forbidden"))

    httpx.post(f"{base_url}/internal/launch/redeem", data={"code": "spent"})

    assert len(failures) == 1
    assert [b.kind for b in failures[0]] == ["address"]


# --- a session is not a licence to authenticate as someone else (SEC-12) ----

def test_a_signed_in_caller_gets_no_exemption_when_guessing_a_password(base_url, held,
                                                                      monkeypatch):
    """The session says who they are, not that THIS attempt succeeded. A route whose
    purpose is to authenticate is judged on its own outcome, so the shared refusal still
    applies to it."""
    from agento.framework.access import sessions
    from agento.web import security
    from agento.web import server as server_module

    monkeypatch.setattr(server_module, "connect", lambda: None)
    monkeypatch.setattr(sessions, "lookup_session", lambda conn, t: _a_session())
    tried: list = []
    monkeypatch.setattr(sessions, "sign_in", lambda conn, u, p: tried.append(u))

    response = httpx.post(f"{base_url}/api/session",
                          json={"username": "victim", "password": "guess"},
                          headers={"Cookie": f"{security.SESSION_COOKIE}=real",
                                   "Origin": "https://panel.localhost:8443",
                                   "Sec-Fetch-Site": "same-origin"})

    assert response.status_code == 429
    assert tried == []


def test_a_signed_in_callers_failed_redemption_is_still_counted(base_url, monkeypatch):
    """Otherwise one account is all it takes to spend forged launch codes uncounted."""
    from agento.framework.access import sessions
    from agento.web import api, rate_limit, security

    monkeypatch.setattr(sessions, "lookup_session", lambda conn, t: _a_session())
    failures: list = []
    monkeypatch.setattr(rate_limit, "record_auth_failure",
                        lambda conn, buckets, **kw: failures.append(buckets))
    monkeypatch.setattr(api, "redeem_launch", lambda req: api.error(403, "forbidden"))

    httpx.post(f"{base_url}/internal/launch/redeem", data={"code": "forged"},
               headers={"Cookie": f"{security.SESSION_COOKIE}=real"})

    assert len(failures) == 1


def test_an_authenticated_request_never_spends_the_shared_address_budget(base_url, counted,
                                                                        monkeypatch):
    """SEC-12: "the address limit counts failures only, so one caller cannot throttle the
    others' authorized traffic".

    The class: a shared counter that authorized traffic can spend. Counting the address on
    every request let one signed-in caller flood it and have every stranger behind that
    address refused - another user's sign-in included.
    """
    from datetime import UTC, datetime, timedelta

    from agento.framework.access import accounts, sessions
    from agento.web import security

    token = "session-token-value"
    session = sessions.Session(
        id="sid", user=accounts.User(id=1, username="root", role="admin", is_active=True),
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))
    monkeypatch.setattr(sessions, "lookup_session",
                        lambda conn, t: session if t == token else None)

    httpx.get(f"{base_url}/api/does-not-exist",
              cookies={security.SESSION_COOKIE: token})

    kinds = [b.kind for batch in counted for b in batch]
    assert "address" not in kinds

    counted.clear()
    httpx.get(f"{base_url}/api/does-not-exist")          # the same path, no identity

    assert "address" in [b.kind for batch in counted for b in batch]


def test_an_uncountable_auth_failure_answers_503_not_401(base_url, counted, monkeypatch,
                                                         secret_file):
    """A limiter that cannot count a failed authentication is not a limiter: answering the
    401 anyway is an unbounded number of free guesses (SEC-12). Fail closed."""
    from agento.web import rate_limit

    def explode(conn, buckets, *, cfg=None):
        raise RuntimeError("the counter is down")

    secret_file.write_text(SECRET)
    monkeypatch.setattr(rate_limit, "record_auth_failure", explode)

    assert httpx.get(f"{base_url}/internal/authz/app").status_code == 503


def test_only_a_resolved_session_sets_the_caller_identity():
    """SEC-12, structurally. `_authenticated` gates the shared bucket and the recording of a
    failed authentication, so anything else that assigns it hands out that exemption. The
    proxy's secret is the case that did: it proves the hop, and it lives in `_proxy_trusted`.

    The shape, not the word: every assignment to `_authenticated` must read the session.
    """
    import ast
    import inspect

    from agento.web import server as module

    tree = ast.parse(inspect.getsource(module))
    assigned = [
        ast.unparse(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and target.attr == "_authenticated"
    ]
    # The reset in __init__ assigns a tuple of literals; every other one reads the session.
    assert assigned, "no assignment found - has the attribute been renamed?"
    assert all("_session" in expr or "False" in expr for expr in assigned), assigned


def test_a_blank_query_parameter_reaches_the_route_as_present(base_url, monkeypatch):
    """`?after=` is present-but-empty: a route that answers 400 for it must SEE it.

    `parse_qsl` drops blank values by default, so the parameter read as absent and got
    the route's default instead — the documented 400 was unreachable from HTTP.
    A repeated parameter resolves to the FIRST value, as `Request.query` documents.
    """
    from agento.web import api

    seen: list[dict] = []

    def echo(req):
        seen.append(dict(req.query))
        return api.Response(200, {})

    route = api.Route("GET", re.compile("^/api/echo$"), echo, auth="login")
    monkeypatch.setattr(api, "ROUTES", [*api.ROUTES, route])
    httpx.get(f"{base_url}/api/echo?after=&n=1&n=2")
    assert seen == [{"after": "", "n": "1"}]
