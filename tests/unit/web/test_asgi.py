"""The FastAPI listener (WS9): SEC-12 runs in one order on every path, and open streams
never starve the requests behind them."""
from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from agento.framework.access import accounts, sessions
from agento.web import api, rate_limit, security
from agento.web.streaming import StreamingResponse

from .conftest import panel_headers
from .test_streaming import TOKEN, _raw_get, _read_headers

SECRET = "a" * 64
SESSION = sessions.Session(
    id="sid", user=accounts.User(id=1, username="root", role="admin", is_active=True),
    expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))


@pytest.fixture
def seen(web, monkeypatch):
    """Every SEC-12 step and the handler, in the order the listener ran them."""
    seen: list[str] = []

    def check(conn, buckets, *, cfg=None):
        seen.append("shared" if any(not b.private for b in buckets) else "private")
        return rate_limit.Decision(True)

    monkeypatch.setattr(rate_limit, "check", check)
    monkeypatch.setattr(rate_limit, "held", lambda conn, buckets: seen.append("held") or 0)
    monkeypatch.setattr(rate_limit, "count_identity",
                        lambda *a, **kw: seen.append("user") or rate_limit.Decision(True))
    monkeypatch.setattr(rate_limit, "record_auth_failure", lambda *a, **kw: seen.append("failure"))
    monkeypatch.setattr(sessions, "lookup_session",
                        lambda conn, t: seen.append("identity") or (SESSION if t == TOKEN else None))
    return seen


def _session_route(response):
    def send(web, monkeypatch, seen, tmp_path):
        route = api.Route("GET", re.compile("^/api/order$"), lambda req: seen.append("handler") or response)
        monkeypatch.setattr(api, "ROUTES", [*api.ROUTES, route])
        return httpx.get(f"{web}/api/order", cookies={security.SESSION_COOKIE: TOKEN})
    return send


def _login(web, monkeypatch, seen, tmp_path):
    monkeypatch.setattr(sessions, "sign_in", lambda conn, u, p: seen.append("handler"))
    return httpx.post(f"{web}/api/session", json={"username": "alice", "password": "x"},
                      headers=panel_headers())


def _redeem(web, monkeypatch, seen, tmp_path):
    monkeypatch.setattr(api, "redeem_launch",
                        lambda req: seen.append("handler") or api.error(403, "forbidden"))
    return httpx.post(f"{web}/internal/launch/redeem", data={"code": "spent"})


def _authz(web, monkeypatch, seen, tmp_path):
    (tmp_path / "proxy-secret").write_text(SECRET)
    monkeypatch.setattr(api, "authorize_app", lambda req: seen.append("handler") or api.Response(200))
    return httpx.get(f"{web}/internal/authz/app", headers={"X-Agento-Proxy-Auth": SECRET})


@pytest.mark.parametrize(("send", "status", "order"), [
    (lambda web, *_: httpx.get(f"{web}/nope"), 404, ["private", "shared"]),
    (_login, 401, ["private", "shared", "handler", "failure"]),
    (_redeem, 403, ["private", "shared", "handler", "failure"]),
    (_authz, 200, ["private", "held", "handler"]),
    (_session_route(api.Response(200, {})), 200, ["private", "identity", "user", "handler"]),
    (_session_route(StreamingResponse(200, iter([b"x"]))), 200,
     ["private", "identity", "user", "handler"]),
])
def test_sec12_runs_in_one_order_before_the_handler(web, monkeypatch, seen, tmp_path,
                                                    send, status, order):
    """Private limiter, identity, stranger refusal, route, then a failure counted (SEC-12)."""
    assert send(web, monkeypatch, seen, tmp_path).status_code == status
    assert seen == order


def _get(web: str, path: str):
    sock = _raw_get(web, path)
    _read_headers(sock)
    return sock


def test_a_dropped_sse_client_releases_its_stream_slot(web, monkeypatch, seen):
    """The real conversation stream gives its §7.3 slot back when the reader leaves."""
    from agento.modules.conversation.src import retention, service, stream

    cfg = {"stream/poll_interval_ms": 10, "stream/heartbeat_seconds": 0, "history/page_size": 50,
           "stream/max_duration_seconds": 60, "stream/max_per_user": 4}
    monkeypatch.setattr(service, "config", lambda conn, path: cfg[path])
    monkeypatch.setattr(service, "load_visible", lambda conn, **kw: {"id": 7})
    monkeypatch.setattr(service, "list_events", lambda conn, **kw: [])
    monkeypatch.setattr(service, "project_events", lambda *a: [])
    monkeypatch.setattr(retention, "cursor_expired", lambda conn, **kw: False)
    route = api.Route("GET", re.compile(r"^/api/threads/(?P<id>[0-9]+)/stream$"), stream.open_stream)
    monkeypatch.setattr(api, "ROUTES", [*api.ROUTES, route])

    sock = _get(web, "/api/threads/7/stream?after=0")
    assert sock.recv(64).startswith(b": ping")
    assert stream.live_streams(SESSION.user.id) == 1
    sock.close()

    deadline = time.monotonic() + 5
    while stream.live_streams(SESSION.user.id) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert stream.live_streams(SESSION.user.id) == 0


def test_sixty_open_streams_leave_the_listener_answering(web, monkeypatch, seen):
    """Streams have their own thread budget: on the request pool (40) they would stop it."""
    stop = threading.Event()

    def frames():
        yield b": ping\n\n"
        stop.wait(10)

    streaming = api.Route("GET", re.compile("^/api/stream$"),
                          lambda req: StreamingResponse(200, frames()))
    plain = api.Route("GET", re.compile("^/api/plain$"), lambda req: api.Response(200, {"ok": True}))
    monkeypatch.setattr(api, "ROUTES", [*api.ROUTES, streaming, plain])
    socks = []
    try:
        socks = [_get(web, "/api/stream") for _ in range(60)]
        assert all(s.recv(64) == b": ping\n\n" for s in socks)
        r = httpx.get(f"{web}/api/plain", cookies={security.SESSION_COOKIE: TOKEN}, timeout=5)
        assert r.json() == {"ok": True}
    finally:
        stop.set()
        for s in socks:
            s.close()
