from __future__ import annotations

import socket
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import uvicorn

from agento.framework.access import accounts, sessions
from agento.web import api, rate_limit, security, server
from tests.waiting import wait_until

PANEL = "https://panel.localhost:8443"
APPS = "https://apps.localhost:8443"
TOKEN = "session-token-value"
ROOT = accounts.User(id=1, username="root", role="admin", is_active=True)


def a_session(user: accounts.User = ROOT) -> sessions.Session:
    return sessions.Session(id="sid", user=user,
                            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))


def sign_in_as(monkeypatch, user: accounts.User = ROOT) -> sessions.Session:
    """`TOKEN`, and only `TOKEN`, resolves to a live session of `user`."""
    session = a_session(user)
    monkeypatch.setattr(sessions, "lookup_session", lambda conn, t: session if t == TOKEN else None)
    return session


def raw_get(base: str, path: str, *, cookie: str = TOKEN) -> socket.socket:
    """A socket we read frame by frame. httpx would hand back the whole body.

    HTTP/1.0, so the body is the raw frames up to the close; HTTP/1.1 wraps them in chunks.
    """
    host, port = base.removeprefix("http://").split(":")
    sock = socket.create_connection((host, int(port)), timeout=5)
    sock.sendall(
        f"GET {path} HTTP/1.0\r\nHost: {host}\r\n"
        f"Cookie: {security.SESSION_COOKIE}={cookie}\r\n\r\n".encode())
    return sock


def read_headers(sock: socket.socket) -> str:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(1)
        if not chunk:
            break
        buf += chunk
    return buf.decode()


@pytest.fixture
def web(monkeypatch, tmp_path, live):
    """A live web server on a free port, with no database: tests patch the access functions."""
    monkeypatch.setenv("AGENTO_PROXY_SECRET_FILE", str(tmp_path / "proxy-secret"))
    monkeypatch.delenv("AGENTO_PROXY_PORT", raising=False)
    monkeypatch.delenv("AGENTO_PANEL_HOST", raising=False)
    monkeypatch.delenv("AGENTO_APPS_HOST", raising=False)
    api.THROTTLE.clear()
    return live


@pytest.fixture(scope="session")
def live():
    """The production app and flags on a free port, once per session: the app reads its
    patched module globals (`connect`, the limiter, `api.ROUTES`) per request."""
    httpd = uvicorn.Server(uvicorn.Config(server.app, port=0, **server.UVICORN_FLAGS))
    thread = threading.Thread(target=httpd.run, daemon=True)
    thread.start()
    wait_until(lambda: httpd.started)
    try:
        yield f"http://127.0.0.1:{httpd.servers[0].sockets[0].getsockname()[1]}"
    finally:
        httpd.should_exit = True
        thread.join(5)


def panel_headers(**extra) -> dict:
    return {"Origin": PANEL, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", **extra}


@pytest.fixture(autouse=True)
def counted(monkeypatch):
    """No database: the limiter ALLOWS and records what it was asked (the real one is tested
    in tests/integration/test_rate_limit.py)."""
    seen: list = []

    def check(conn, buckets, *, cfg=None):
        seen.append(buckets)
        return rate_limit.Decision(True)

    monkeypatch.setattr(server, "connect", lambda: MagicMock(name="conn"))
    monkeypatch.setattr(rate_limit, "check", check)
    monkeypatch.setattr(rate_limit, "held", lambda conn, buckets: 0)
    monkeypatch.setattr(rate_limit, "count_identity", lambda *a, **kw: rate_limit.Decision(True))
    monkeypatch.setattr(rate_limit, "record_auth_failure", lambda *a, **kw: None)
    return seen
