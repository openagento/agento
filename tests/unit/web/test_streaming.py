"""The streaming response contract (PRD E3-E5 §7.1).

Asserted against a real socket, not a mock writer: "the frames reach the client as they are
produced" is a property of the listener plus the connection, and a fake `wfile` would pass
whatever we wrote into it.
"""
from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from agento.web import api, rate_limit, security
from agento.web.streaming import StreamingResponse, frame, sse
from tests.waiting import wait_until

from .conftest import TOKEN, panel_headers, raw_get, read_headers, sign_in_as


@pytest.fixture
def signed_in(monkeypatch):
    return sign_in_as(monkeypatch)


def _route(monkeypatch, handler, *, method="GET", path="/api/stream/test"):
    import re
    route = api.Route(method, re.compile(f"^{path}$"), handler, acl="self")
    monkeypatch.setattr(api, "ROUTES", [*api.ROUTES, route])
    return path


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
    sock = raw_get(web, path)
    try:
        read_headers(sock)
        assert sock.recv(64) == b"first\n"       # arrives while the handler is blocked
        release.set()
        assert sock.recv(64) == b"second\n"
    finally:
        sock.close()
        release.set()


@pytest.mark.parametrize(("handler", "status", "present", "absent"), [
    pytest.param(lambda req: StreamingResponse(200, iter([b"x"])), 200,
                 ["connection: close"], ["content-length"], id="no-content-length"),
    pytest.param(lambda req: sse(iter([frame(data="hi")])), 200,
                 ["cache-control: no-store", "x-content-type-options: nosniff",
                  "content-type: text/event-stream", "x-accel-buffering: no"], [],
                 id="same-security-headers-as-any-reply"),
    pytest.param(lambda req: StreamingResponse(200, iter([b"x"]), headers=[("X-Demo", "1")]), 200,
                 ["x-demo: 1"], [], id="the-streams-own-headers"),
    pytest.param(lambda req: StreamingResponse(503, iter([b""])), 503, [], [], id="the-handlers-status"),
])
def test_a_streams_head(web, monkeypatch, signed_in, handler, status, present, absent):
    sock = raw_get(web, _route(monkeypatch, handler))
    try:
        headers = read_headers(sock).lower()
    finally:
        sock.close()

    assert headers.startswith(f"http/1.1 {status} ")
    assert [h for h in present if h not in headers] == []
    assert [h for h in absent if h in headers] == []


# --- a dropped client ------------------------------------------------------

def _endless():
    while True:
        yield b"tick\n"
        time.sleep(0.01)


@pytest.mark.parametrize(("frames", "keep", "read_all"), [
    pytest.param(_endless, False, False, id="dropped-client"),
    # A strong reference to the response outlives the request, so refcounting never collects
    # the generator: a release that happens only because CPython dropped the last reference
    # is not a release. The listener's close must do it.
    pytest.param(_endless, True, False, id="dropped-client-not-the-garbage-collector"),
    # `close()` on an already-exhausted generator is a no-op: the same one release.
    pytest.param(lambda: iter([b"only\n"]), False, True, id="clean-end"),
])
def test_the_handlers_finally_runs_exactly_once(web, monkeypatch, signed_in, frames, keep, read_all):
    """The §7.3 slot is released in that `finally`. Once, or the count drifts either way."""
    released, kept = [], []

    def handler(req):
        def wrapped():
            try:
                yield from frames()
            finally:
                released.append(1)
        response = StreamingResponse(200, wrapped())
        if keep:
            kept.append(response)
        return response

    sock = raw_get(web, _route(monkeypatch, handler))
    try:
        read_headers(sock)
        assert sock.recv(64)
        while read_all and sock.recv(64):
            pass
    finally:
        sock.close()

    assert wait_until(lambda: released)
    assert released == [1]
    assert len(kept) == keep


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
    sock = raw_get(web, path)
    try:
        read_headers(sock)
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
    sock = raw_get(web, path)
    try:
        read_headers(sock)
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

@pytest.mark.parametrize(("kwargs", "expected"), [
    pytest.param({"data": "hello"}, b"data: hello\n\n", id="one-data-line-and-a-blank-line"),
    # A raw newline would end the frame early - the standard's own answer, not ours.
    pytest.param({"data": "a\nb"}, b"data: a\ndata: b\n\n", id="one-data-line-per-line"),
    pytest.param({"data": "{}", "event": "delta", "event_id": 42}, b"event: delta\nid: 42\ndata: {}\n\n",
                 id="event-name-and-id"),
])
def test_the_frame_format(kwargs, expected):
    assert frame(**kwargs) == expected


def test_the_id_is_what_a_browser_resends_as_last_event_id():
    """`id:` is the whole reconnect story (WHATWG HTML 9.2.3): the cursor needs no client
    code, so a frame carrying an event's row id is what makes replay automatic."""
    payload = json.dumps({"kind": "assistant.message"})

    assert frame(data=payload, event_id=7).startswith(b"id: 7\n")


def test_sse_declares_the_stream_content_type():
    response = sse(iter([]))

    assert response.status == 200
    assert ("Content-Type", "text/event-stream") in response.headers
