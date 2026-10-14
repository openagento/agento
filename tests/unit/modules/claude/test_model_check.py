"""Claude ``check_model``: a real child process, started by an in-process runner (WS5),
against real local sockets (TST-3)."""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
import respx

from agento.framework.agent_manager.models import CredentialRecord
from agento.framework.config_test import ERROR, FAIL, OK
from agento.modules.claude.src import model_check
from agento.modules.claude.src.model_check import check_model
from agento.modules.claude.src.model_probe import probe

pytestmark = pytest.mark.usefixtures("runner_server")

API_KEY = CredentialRecord(id=1, scope="claude", type="anthropic_api_key", label="key",
                           credentials={"api_key": "sk-ant-test"})
OAUTH = CredentialRecord(id=2, scope="claude", type="oauth", label="sub",
                         credentials={"subscription_key": "oauth-token-test"})


@pytest.fixture
def api(monkeypatch):
    """A local API: 200 for claude-known, 401 for claude-denied, 404 otherwise."""
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({"path": self.path, **{k.lower(): v for k, v in self.headers.items()}})
            code = {"/v1/models/claude-known": 200, "/v1/models/claude-denied": 401}.get(self.path, 404)
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b'{"body": "never read"}')

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(model_check, "API_BASE", f"http://127.0.0.1:{server.server_port}")
    yield seen
    server.shutdown()


@pytest.fixture
def slow_headers(monkeypatch):
    """Accepts, then sends response headers one byte every 50 ms, forever."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()

    def serve():
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            try:
                conn.recv(4096)
                for b in b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 10_000:
                    if stop.is_set():
                        break
                    conn.send(bytes([b]))
                    time.sleep(0.05)
            except OSError:
                pass
            finally:
                conn.close()

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setattr(model_check, "API_BASE", f"http://127.0.0.1:{listener.getsockname()[1]}")
    yield
    stop.set()
    listener.close()


@pytest.fixture
def spawned(monkeypatch):
    """Pids of the processes the check starts."""
    pids: list[int] = []

    class Recording(subprocess.Popen):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            pids.append(self.pid)

    monkeypatch.setattr(subprocess, "Popen", Recording)
    return pids


def _assert_reaped(pids):
    assert pids
    for pid in pids:
        with pytest.raises(ChildProcessError):  # killed AND waited for
            os.waitpid(pid, os.WNOHANG)


def test_known_model_with_api_key(api):
    result = check_model("anthropic", "claude-known", API_KEY, timeout_s=10)
    assert (result.status, result.code) == (OK, "MODEL_OK")
    assert api[0]["x-api-key"] == "sk-ant-test"


def test_unknown_model_with_oauth(api):
    result = check_model("anthropic", "claude-nope", OAUTH, timeout_s=10)
    assert (result.status, result.code) == (FAIL, "MODEL_UNKNOWN")
    assert api[0]["authorization"] == "Bearer oauth-token-test"
    assert api[0]["anthropic-beta"] == "oauth-2025-04-20"


def test_other_status_is_could_not_check(api):
    result = check_model("anthropic", "claude-denied", OAUTH, timeout_s=10)
    assert (result.status, result.code) == (ERROR, "MODEL_CHECK_FAILED")
    assert "HTTP 401" in result.message


def test_transport_error_is_could_not_check(monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here
    monkeypatch.setattr(model_check, "API_BASE", f"http://127.0.0.1:{port}")
    result = check_model("anthropic", "claude-known", API_KEY, timeout_s=10)
    assert (result.status, result.code) == (ERROR, "MODEL_CHECK_FAILED")


@pytest.mark.parametrize("model, code", [
    ("sonnet", "MODEL_OK"),
    ("claude/../x", "MODEL_UNKNOWN"),
    ("a?b", "MODEL_UNKNOWN"),
    ("a#b", "MODEL_UNKNOWN"),
    ("a%2Fb", "MODEL_UNKNOWN"),
    ("has space", "MODEL_UNKNOWN"),
])
def test_alias_and_invalid_ids_make_no_request(monkeypatch, model, code):
    def no_spawn(*_a, **_k):
        raise AssertionError("no request for an alias or an invalid id")

    monkeypatch.setattr(model_check.runner_client, "run", no_spawn)
    assert check_model("anthropic", model, API_KEY, timeout_s=10).code == code


@respx.mock
def test_probe_never_reads_the_body():
    class Unreadable(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("the body was read")

    respx.get("https://api.example.com/v1/models/m").mock(
        return_value=httpx.Response(200, stream=Unreadable()),
    )
    assert probe("https://api.example.com/v1/models/m", {}) == 200


def test_slow_headers_end_at_the_deadline(slow_headers, spawned):
    started = time.monotonic()
    result = check_model("anthropic", "claude-known", API_KEY, timeout_s=0.5)
    assert time.monotonic() - started < 1.0
    assert (result.status, result.code) == (ERROR, "MODEL_CHECK_TIMEOUT")
    _assert_reaped(spawned)


def test_blocking_dns_ends_at_the_deadline(tmp_path, monkeypatch, spawned):
    (tmp_path / "blocking_dns_probe.py").write_text(
        "import socket, time\n"
        "socket.getaddrinfo = lambda *a, **k: time.sleep(30)\n"
        "from agento.modules.claude.src.model_probe import main\n"
        "main()\n"
    )
    monkeypatch.setenv("PYTHONPATH", f"{tmp_path}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    monkeypatch.setattr(model_check, "PROBE_MODULE", "blocking_dns_probe")
    started = time.monotonic()
    result = check_model("anthropic", "claude-known", API_KEY, timeout_s=0.5)
    assert time.monotonic() - started < 1.0
    assert (result.status, result.code) == (ERROR, "MODEL_CHECK_TIMEOUT")
    _assert_reaped(spawned)


def test_credential_reaches_the_child_only_on_stdin(api, monkeypatch):
    calls = []
    real_run = model_check.runner_client.run

    def spy(args, **kwargs):
        calls.append((args, kwargs))
        return real_run(args, **kwargs)

    monkeypatch.setattr(model_check.runner_client, "run", spy)
    check_model("anthropic", "claude-known", API_KEY, timeout_s=10)
    (args, kwargs), = calls
    assert not any("sk-ant-test" in a for a in args)
    assert "sk-ant-test" in kwargs["input"]
    assert "env" not in kwargs
