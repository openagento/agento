from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest

from agento.web import api, rate_limit, server
from agento.web.server import Handler

PANEL = "https://panel.localhost:8443"
APPS = "https://apps.localhost:8443"


@pytest.fixture
def web(monkeypatch, tmp_path):
    """A live web server on a free port, with no database: tests patch the access functions."""
    monkeypatch.setenv("AGENTO_PROXY_SECRET_FILE", str(tmp_path / "proxy-secret"))
    monkeypatch.delenv("AGENTO_PROXY_PORT", raising=False)
    monkeypatch.delenv("AGENTO_PANEL_HOST", raising=False)
    monkeypatch.delenv("AGENTO_APPS_HOST", raising=False)
    monkeypatch.setattr(server, "connect", lambda: MagicMock(name="conn"))
    api.THROTTLE.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def panel_headers(**extra) -> dict:
    return {"Origin": PANEL, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", **extra}


@pytest.fixture(autouse=True)
def counted(monkeypatch):
    """The limiter needs a database; these tests have none.

    Stubbed to ALLOW and to record what it was asked, so a test can still assert that a
    request reached the limiter at all. The limiter's own behaviour is tested against real
    MySQL in tests/integration/test_rate_limit.py.
    """
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
