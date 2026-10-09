from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest
import uvicorn

from agento.web import api, rate_limit, server

PANEL = "https://panel.localhost:8443"
APPS = "https://apps.localhost:8443"


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
    deadline = time.monotonic() + 5
    while not httpd.started and time.monotonic() < deadline:
        time.sleep(0.01)
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
