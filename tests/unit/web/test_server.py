"""Health, and the authorization endpoints only the proxy can reach.

`toolbox` shares db-net with `web`, so a direct call from it looks exactly like a
request with no (or a forged) X-Agento-Proxy-Auth header — the toolbox cannot read the
secret, because only `proxy` and `web` mount its volume (asserted in test_provisioning).
"""
from __future__ import annotations

import re

import httpx
import pytest

from agento.framework.access import accounts
from agento.web import security

from .conftest import PANEL, TOKEN, sign_in_as

SECRET = "a" * 64
APP = "/a/demo/v/v-20260925-120000-ab12/index.html"
ALICE = accounts.User(id=1, username="alice", role="user", is_active=True)


@pytest.fixture
def secret_file(web, tmp_path):
    """The file the `web` fixture points AGENTO_PROXY_SECRET_FILE at; absent until written."""
    return tmp_path / "proxy-secret"


def test_health_answers_ok(web):
    r = httpx.get(f"{web}/health")
    assert r.status_code == 200
    assert r.text == "ok"


@pytest.mark.parametrize(("secret", "headers"), [
    pytest.param(SECRET, {}, id="direct-call-without-the-secret"),
    pytest.param(SECRET, {"X-Agento-Proxy-Auth": "b" * 64}, id="forged-secret"),
    pytest.param(SECRET, {"X-Agento-User": "1", "X-Forwarded-User": "admin"},
                 id="caller-identity-headers-without-the-secret"),
    pytest.param(None, {"X-Agento-Proxy-Auth": ""}, id="missing-secret-on-web-fails-closed"),
])
def test_app_authz_without_the_proxy_secret_is_denied(web, secret_file, secret, headers):
    if secret:
        secret_file.write_text(secret)
    assert httpx.get(f"{web}/internal/authz/app", headers=headers).status_code == 401


def test_there_is_no_share_authz_endpoint(web, secret_file):
    # Shares are checked by the artifacts server (DECISIONS 2026-09-27, E6 S1).
    secret_file.write_text(SECRET)
    assert httpx.get(f"{web}/internal/authz/share", headers={"X-Agento-Proxy-Auth": SECRET}).status_code == 404


def test_the_proxy_gets_a_deny_without_a_launch_cookie(web, secret_file):
    secret_file.write_text(SECRET)
    r = httpx.get(f"{web}/internal/authz/app", headers={"X-Agento-Proxy-Auth": SECRET, "X-Forwarded-Uri": APP})
    assert r.status_code == 403


@pytest.mark.parametrize("uri", [None, "/a/demo/", "/a/demo/current/index.html",
                                 "/a/demo/v/v-20260925-120000-ab12/%2e%2e/other/index.html"])
def test_a_path_that_is_not_a_version_path_is_404_without_a_launch_cookie(web, secret_file, uri):
    # The proxy serves only /a/<code>/v/<id>/ (PRD E6 §6.2): the path decides before the cookie.
    secret_file.write_text(SECRET)
    headers = {"X-Agento-Proxy-Auth": SECRET} | ({"X-Forwarded-Uri": uri} if uri else {})
    assert httpx.get(f"{web}/internal/authz/app", headers=headers).status_code == 404


def test_secret_written_after_start_is_picked_up(web, secret_file):
    headers = {"X-Agento-Proxy-Auth": SECRET, "X-Forwarded-Uri": APP}
    assert httpx.get(f"{web}/internal/authz/app", headers=headers).status_code == 401
    secret_file.write_text(SECRET + "\n")
    assert httpx.get(f"{web}/internal/authz/app", headers=headers).status_code == 403


def test_unknown_path_is_404(web):
    assert httpx.get(f"{web}/login").status_code == 404


def test_query_string_never_reaches_the_log(web, capfd):
    httpx.get(f"{web}/internal/authz/app?cap=SECRETCAP&code=SECRETCODE")
    httpx.get(f"{web}/health?cap=SECRETCAP")
    err = capfd.readouterr().err
    assert "/internal/authz/app" in err
    assert "SECRET" not in err


def test_a_request_to_an_unknown_path_is_still_counted(web, counted):
    """The limiter is mounted before dispatch, so a route cannot escape it by not existing."""
    assert httpx.get(f"{web}/nope").status_code == 404
    assert [b.kind for b in counted[-1]] == ["address"]


def test_health_is_not_counted(web, counted):
    assert httpx.get(f"{web}/health").status_code == 200
    assert counted == []


def test_the_limiter_fails_closed_when_it_cannot_count(web, monkeypatch):
    from agento.web import rate_limit

    monkeypatch.setattr(rate_limit, "check", _raise)
    assert httpx.get(f"{web}/api/session").status_code == 503


def _raise(*args, **kwargs):
    raise RuntimeError("no database")


def test_every_401_is_recorded_as_a_failed_authentication(web, monkeypatch, secret_file):
    """A failed login, a probe without the proxy secret and a dead session all feed the hold."""
    from agento.web import rate_limit

    failures: list = []
    monkeypatch.setattr(rate_limit, "record_auth_failure", lambda conn, buckets, **kw: failures.append(buckets))
    secret_file.write_text(SECRET)

    httpx.get(f"{web}/internal/authz/app")          # no proxy secret -> 401
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
def test_a_held_address_is_refused_whatever_cookie_it_presents(web, held, cookie):
    """Presenting a credential is not proving one.

    The hold exists because this address just failed to authenticate repeatedly, which is
    what a brute force looks like; if a cookie the server never validated counted as
    authentication, every hold would have a one-header bypass.
    """
    headers = {"Cookie": cookie} if cookie else {}
    response = httpx.get(f"{web}/api/does-not-exist", headers=headers)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "900"


def test_a_held_address_still_reaches_health(web, held):
    """The one exemption stays exempt: a held address must not make the container unhealthy."""
    assert httpx.get(f"{web}/health").status_code == 200


# --- a shared refusal costs the authorized caller nothing, and the stranger no side
#     effect (SEC-12) --------------------------------------------------------

def test_an_authenticated_caller_survives_another_callers_shared_address_refusal(
        web, held, monkeypatch):
    """SEC-12: "the address limit counts failures only, so one caller cannot throttle the
    others' authorized traffic". Behind one NAT, the brute force and the colleague share an
    address; refusing the colleague is the attacker throttling them."""
    sign_in_as(monkeypatch, ALICE)
    monkeypatch.setattr(accounts, "held_admin", lambda conn, role: [])

    response = httpx.get(f"{web}/api/session", cookies={security.SESSION_COOKIE: TOKEN})

    assert response.status_code == 200


@pytest.mark.parametrize("signed_in", [pytest.param(False, id="stranger"),
                                       pytest.param(True, id="signed-in-caller")])
def test_a_shared_refusal_lands_before_the_password_is_derived(web, held, monkeypatch, signed_in):
    """The refusal is a decision about whether the handler runs, not about which body is
    written afterwards. A 429 that replaces the answer has already paid for the work.

    A session is not a licence to authenticate as someone else (SEC-12): it says who they
    are, not that THIS attempt succeeded. A route whose purpose is to authenticate is judged
    on its own outcome, so the shared refusal still applies to a signed-in caller.
    """
    from agento.framework.access import sessions

    if signed_in:
        sign_in_as(monkeypatch, ALICE)
    tried: list = []
    monkeypatch.setattr(sessions, "sign_in", lambda conn, u, p: tried.append(u))

    response = httpx.post(f"{web}/api/session", json={"username": "victim", "password": "guess"},
                          headers={"Origin": PANEL, "Sec-Fetch-Site": "same-origin"},
                          cookies={security.SESSION_COOKIE: TOKEN} if signed_in else {})

    assert response.status_code == 429
    assert tried == []


def test_a_shared_refusal_consumes_no_launch_code(web, held, monkeypatch):
    """Redemption spends a single-use code and commits. A refusal that arrives after it has
    burned the caller's only code and handed them a 429 for it."""
    from agento.web import api

    redeemed: list = []
    monkeypatch.setattr(api, "redeem_launch", lambda req: redeemed.append(req.body))

    response = httpx.post(f"{web}/internal/launch/redeem", data={"code": "one-use"})

    assert response.status_code == 429
    assert redeemed == []


@pytest.mark.parametrize(("signed_in", "kinds"), [
    pytest.param(False, ["address"], id="stranger"),
    # Otherwise one account is all it takes to spend forged launch codes uncounted.
    pytest.param(True, ["address", "session"], id="signed-in-caller"),
])
def test_an_invalid_launch_redemption_is_counted_as_a_failed_authentication(
        web, monkeypatch, signed_in, kinds):
    """A spent or forged exchange code is answered 403, not 401 - and a flood of them is a
    brute force whichever status the route reached for (SEC-12 names both)."""
    from agento.web import api, rate_limit

    if signed_in:
        sign_in_as(monkeypatch, ALICE)
    failures: list = []
    monkeypatch.setattr(rate_limit, "record_auth_failure",
                        lambda conn, buckets, **kw: failures.append(buckets))
    monkeypatch.setattr(api, "redeem_launch", lambda req: api.error(403, "forbidden"))

    httpx.post(f"{web}/internal/launch/redeem", data={"code": "forged"},
               cookies={security.SESSION_COOKIE: TOKEN} if signed_in else {})

    assert [[b.kind for b in batch] for batch in failures] == [kinds]


def test_an_authenticated_request_never_spends_the_shared_address_budget(web, counted,
                                                                        monkeypatch):
    """SEC-12: "the address limit counts failures only, so one caller cannot throttle the
    others' authorized traffic".

    The class: a shared counter that authorized traffic can spend. Counting the address on
    every request let one signed-in caller flood it and have every stranger behind that
    address refused - another user's sign-in included.
    """
    sign_in_as(monkeypatch)

    httpx.get(f"{web}/api/does-not-exist", cookies={security.SESSION_COOKIE: TOKEN})

    kinds = [b.kind for batch in counted for b in batch]
    assert "address" not in kinds

    counted.clear()
    httpx.get(f"{web}/api/does-not-exist")          # the same path, no identity

    assert "address" in [b.kind for batch in counted for b in batch]


def test_an_uncountable_auth_failure_answers_503_not_401(web, counted, monkeypatch,
                                                         secret_file):
    """A limiter that cannot count a failed authentication is not a limiter: answering the
    401 anyway is an unbounded number of free guesses (SEC-12). Fail closed."""
    from agento.web import rate_limit

    def explode(conn, buckets, *, cfg=None):
        raise RuntimeError("the counter is down")

    secret_file.write_text(SECRET)
    monkeypatch.setattr(rate_limit, "record_auth_failure", explode)

    assert httpx.get(f"{web}/internal/authz/app").status_code == 503


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


def test_a_blank_query_parameter_reaches_the_route_as_present(web, monkeypatch):
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

    route = api.Route("GET", re.compile("^/api/echo$"), echo, auth="login", acl="anonymous")
    monkeypatch.setattr(api, "ROUTES", [*api.ROUTES, route])
    httpx.get(f"{web}/api/echo?after=&n=1&n=2")
    assert seen == [{"after": "", "n": "1"}]
