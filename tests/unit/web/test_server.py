"""Health, and the authorization endpoints only the proxy can reach.

`sandbox` shares agento-net with `web`, so a direct call from it looks exactly like a
request with no (or a forged) X-Agento-Proxy-Auth header — the sandbox cannot read the
secret, because only `proxy` and `web` mount its volume (asserted in test_provisioning).
"""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from agento.web.server import Handler

SECRET = "a" * 64


@pytest.fixture
def secret_file(tmp_path, monkeypatch):
    path = tmp_path / "proxy-secret"
    monkeypatch.setenv("AGENTO_PROXY_SECRET_FILE", str(path))
    return path


@pytest.fixture
def base_url(secret_file):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_health_answers_ok(base_url):
    r = httpx.get(f"{base_url}/health")
    assert r.status_code == 200
    assert r.text == "ok"


@pytest.mark.parametrize("path", ["/internal/authz/app", "/internal/authz/share"])
def test_direct_call_without_the_proxy_secret_is_denied(base_url, secret_file, path):
    secret_file.write_text(SECRET)
    assert httpx.get(f"{base_url}{path}").status_code == 401


def test_forged_proxy_secret_is_denied(base_url, secret_file):
    secret_file.write_text(SECRET)
    r = httpx.get(f"{base_url}/internal/authz/app", headers={"X-Agento-Proxy-Auth": "b" * 64})
    assert r.status_code == 401


def test_caller_identity_headers_without_the_secret_are_denied(base_url, secret_file):
    secret_file.write_text(SECRET)
    r = httpx.get(f"{base_url}/internal/authz/app",
                  headers={"X-Agento-User": "1", "X-Forwarded-User": "admin"})
    assert r.status_code == 401


@pytest.mark.parametrize("path", ["/internal/authz/app", "/internal/authz/share"])
def test_the_proxy_gets_a_deny_without_a_launch_cookie(base_url, secret_file, path):
    secret_file.write_text(SECRET)
    r = httpx.get(f"{base_url}{path}", headers={"X-Agento-Proxy-Auth": SECRET})
    assert r.status_code == 403


def test_missing_secret_on_web_fails_closed(base_url, secret_file):
    r = httpx.get(f"{base_url}/internal/authz/app", headers={"X-Agento-Proxy-Auth": ""})
    assert r.status_code == 401


def test_secret_written_after_start_is_picked_up(base_url, secret_file):
    headers = {"X-Agento-Proxy-Auth": SECRET}
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
