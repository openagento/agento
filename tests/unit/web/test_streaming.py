"""The streaming response contract (PRD E3-E5 §7.1).

Asserted against a real socket, not a mock writer: "the frames reach the client as they are
produced" is a property of the listener plus the connection, and a fake `wfile` would pass
whatever we wrote into it.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from agento.web import api, rate_limit, security
from agento.web.streaming import StreamingResponse, frame, sse

from .conftest import panel_headers

TOKEN = "session-token-value"


@pytest.fixture
def signed_in(monkeypatch):
    from agento.framework.access import accounts, sessions

    session = sessions.Session(
        id="sid", user=accounts.User(id=1, username="root", role="admin", is_active=True),
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))
    monkeypatch.setattr(sessions, "lookup_session", lambda conn, t: session if t == TOKEN else None)
    return session


def _route(monkeypatch, handler, *, method="GET", path="/api/stream/test"):
    import re
    route = api.Route(method, re.compile(f"^{path}$"), handler)
    monkeypatch.setattr(api, "ROUTES", [*api.ROUTES, route])
    return path


def _raw_get(base: str, path: str, *, cookie: str = TOKEN) -> socket.socket:
    """A socket we read frame by frame. httpx would hand back the whole body."""
    host, port = base.removeprefix("http://").split(":")
    sock = socket.create_connection((host, int(port)), timeout=5)
    sock.sendall(
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
        f"Cookie: {security.SESSION_COOKIE}={cookie}\r\n\r\n".encode())
    return sock


def _read_headers(sock: socket.socket) -> str:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(1)
        if not chunk:
            break
        buf += chunk
    return buf.decode()


# --- frames reach the socket incrementally ---------------------------------

def test_frames_arrive_before_the_generator_has_finished(web, monkeypatch, signed_in):
    """The point of a stream: the first frame is readable while the handler still runs."""
    release = threading.Event()

    def handler(req):
        def frames():
            yield b"first\n"
            release.wait(5)
            yield b"second\n"
        return StreamingResponse(200, frames())

    path = _route(monkeypatch, handler)
    sock = _raw_get(web, path)
    try:
        _read_headers(sock)
        assert sock.recv(64) == b"first\n"       # arrives while the handler is blocked
        release.set()
        assert sock.recv(64) == b"second\n"
    finally:
        sock.close()
        release.set()


def test_a_stream_sends_no_content_length(web, monkeypatch, signed_in):
    def handler(req):
        return StreamingResponse(200, iter([b"x"]))

    path = _route(monkeypatch, handler)
    sock = _raw_get(web, path)
    try:
        headers = _read_headers(sock).lower()
    finally:
        sock.close()

    assert "content-length" not in headers
    assert "connection: close" in headers


def test_a_stream_carries_the_same_security_headers_as_any_reply(web, monkeypatch, signed_in):
    path = _route(monkeypatch, lambda req: sse(iter([frame(data="hi")])))
    sock = _raw_get(web, path)
    try:
        headers = _read_headers(sock).lower()
    finally:
        sock.close()

    assert "cache-control: no-store" in headers
    assert "x-content-type-options: nosniff" in headers
    assert "content-type: text/event-stream" in headers
    assert "x-accel-buffering: no" in headers


def test_a_streams_own_headers_are_sent(web, monkeypatch, signed_in):
    path = _route(monkeypatch, lambda req: StreamingResponse(
        200, iter([b"x"]), headers=[("X-Demo", "1")]))
    sock = _raw_get(web, path)
    try:
        assert "x-demo: 1" in _read_headers(sock).lower()
    finally:
        sock.close()


def test_the_status_line_is_the_handlers(web, monkeypatch, signed_in):
    path = _route(monkeypatch, lambda req: StreamingResponse(503, iter([b""])))
    sock = _raw_get(web, path)
    try:
        assert _read_headers(sock).startswith("HTTP/1.0 503")
    finally:
        sock.close()


# --- a dropped client ------------------------------------------------------

def test_a_dropped_client_runs_the_handlers_finally_exactly_once(web, monkeypatch, signed_in):
    """The §7.3 slot is released in that `finally`. Once, or the count drifts either way."""
    released = []
    started = threading.Event()

    def handler(req):
        def frames():
            try:
                started.set()
                while True:
                    yield b"tick\n"
                    time.sleep(0.01)
            finally:
                released.append(1)
        return StreamingResponse(200, frames())

    path = _route(monkeypatch, handler)
    sock = _raw_get(web, path)
    _read_headers(sock)
    assert started.wait(5)
    sock.recv(64)
    sock.close()

    deadline = time.monotonic() + 5
    while not released and time.monotonic() < deadline:
        time.sleep(0.02)

    assert released == [1]


def test_the_release_is_the_listeners_close_and_not_the_garbage_collector(web, monkeypatch,
                                                                            signed_in):
    """A strong reference to the response outlives the request, so refcounting will never
    collect the generator. The slot must still be released - a release that happens only
    because CPython happened to drop the last reference is not a release."""
    released, kept = [], []

    def handler(req):
        def frames():
            try:
                while True:
                    yield b"tick\n"
                    time.sleep(0.01)
            finally:
                released.append(1)
        response = StreamingResponse(200, frames())
        kept.append(response)
        return response

    path = _route(monkeypatch, handler)
    sock = _raw_get(web, path)
    _read_headers(sock)
    sock.recv(64)
    sock.close()

    deadline = time.monotonic() + 5
    while not released and time.monotonic() < deadline:
        time.sleep(0.02)

    assert kept and released == [1]


def test_a_clean_end_runs_the_finally_exactly_once(web, monkeypatch, signed_in):
    """`close()` on an already-exhausted generator is a no-op: the same one release."""
    released = []

    def handler(req):
        def frames():
            try:
                yield b"only\n"
            finally:
                released.append(1)
        return StreamingResponse(200, frames())

    path = _route(monkeypatch, handler)
    sock = _raw_get(web, path)
    try:
        _read_headers(sock)
        while sock.recv(64):
            pass
    finally:
        sock.close()

    deadline = time.monotonic() + 5
    while not released and time.monotonic() < deadline:
        time.sleep(0.02)

    assert released == [1]


def test_a_handler_that_raises_mid_stream_still_releases(web, monkeypatch, signed_in):
    released = []

    def handler(req):
        def frames():
            try:
                yield b"first\n"
                raise RuntimeError("the source died")
            finally:
                released.append(1)
        return StreamingResponse(200, frames())

    path = _route(monkeypatch, handler)
    sock = _raw_get(web, path)
    try:
        _read_headers(sock)
        assert sock.recv(64) == b"first\n"
        assert sock.recv(64) == b""              # cut, not a second set of headers
    finally:
        sock.close()

    assert released == [1]


# --- a streaming route is an ordinary route --------------------------------

def test_an_unauthenticated_stream_is_401_and_never_reaches_the_handler(web, monkeypatch):
    from agento.framework.access import sessions

    monkeypatch.setattr(sessions, "lookup_session", lambda conn, t: None)
    reached = []
    path = _route(monkeypatch, lambda req: reached.append(1) or StreamingResponse(200, iter([])))

    r = httpx.get(f"{web}{path}")

    assert r.status_code == 401
    assert reached == []


def test_a_stream_passes_through_the_same_limiter(web, monkeypatch, signed_in, counted):
    path = _route(monkeypatch, lambda req: StreamingResponse(200, iter([b"x"])))
    sock = _raw_get(web, path)
    try:
        _read_headers(sock)
    finally:
        sock.close()

    # The SESSION bucket only: an authenticated request never spends the shared address
    # budget, or one signed-in caller could lock out every stranger behind that address,
    # a sign-in included (SEC-12, "the address limit counts failures only").
    assert [b.kind for b in counted[-1]] == ["session"]


def test_a_shared_hold_does_not_stop_a_caller_that_signed_in(web, monkeypatch, signed_in):
    """The other half of the hold rule (§7.5).

    Many callers sit behind one address - today, behind one proxy. A hold earned by one
    stranger's failed logins must not lock out everyone who really is signed in, or one
    caller could throttle every other one at will.
    """
    monkeypatch.setattr(rate_limit, "check",
                        lambda conn, buckets, *, cfg=None: rate_limit.Decision(True, 900, 900))
    path = _route(monkeypatch, lambda req: api.Response(200, {"ok": True}))

    r = httpx.get(f"{web}{path}", cookies={security.SESSION_COOKIE: TOKEN})

    assert r.status_code == 200


def test_a_refused_limiter_answers_429_and_not_a_stream(web, monkeypatch, signed_in):
    monkeypatch.setattr(rate_limit, "count_identity",
                        lambda *a, **kw: rate_limit.Decision(False, retry_after=30))
    reached = []
    path = _route(monkeypatch, lambda req: reached.append(1) or StreamingResponse(200, iter([])))

    r = httpx.get(f"{web}{path}", cookies={security.SESSION_COOKIE: TOKEN})

    assert (r.status_code, r.headers["Retry-After"]) == (429, "30")
    assert reached == []


def test_a_streaming_write_route_still_needs_its_csrf_token(web, monkeypatch, signed_in):
    reached = []
    path = _route(monkeypatch,
                  lambda req: reached.append(1) or StreamingResponse(200, iter([])),
                  method="POST", path="/api/stream/write")

    r = httpx.post(f"{web}{path}", headers=panel_headers(),
                   cookies={security.SESSION_COOKIE: TOKEN})

    assert r.status_code == 403
    assert reached == []


# --- the frame format ------------------------------------------------------

def test_a_frame_is_one_data_line_and_a_blank_line():
    assert frame(data="hello") == b"data: hello\n\n"


def test_a_multi_line_body_is_one_data_line_per_line():
    """A raw newline would end the frame early - the standard's own answer, not ours."""
    assert frame(data="a\nb") == b"data: a\ndata: b\n\n"


def test_a_frame_can_carry_an_event_name_and_an_id():
    assert frame(data="{}", event="delta", event_id=42) == b"event: delta\nid: 42\ndata: {}\n\n"


def test_the_id_is_what_a_browser_resends_as_last_event_id():
    """`id:` is the whole reconnect story (WHATWG HTML 9.2.3): the cursor needs no client
    code, so a frame carrying an event's row id is what makes replay automatic."""
    payload = json.dumps({"kind": "assistant.message"})

    assert frame(data=payload, event_id=7).startswith(b"id: 7\n")


def test_sse_declares_the_stream_content_type():
    response = sse(iter([]))

    assert response.status == 200
    assert ("Content-Type", "text/event-stream") in response.headers
