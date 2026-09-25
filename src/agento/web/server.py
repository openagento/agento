"""E1.5 web scaffold: a health check and the authorization endpoints the proxy calls.

Login, sessions and RBAC are E2's; the allow/deny decision behind /internal/authz/* is
E2's (apps) and E6's (share). Until then every authorized subrequest is denied.

`web` shares agento-net with `sandbox`, so reachability proves nothing: a request is from
the proxy only if it carries the secret the proxy and web alone can read. Nothing on this
listener may trust an identity or forwarding header without that check.
"""
from __future__ import annotations

import hmac
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

AUTHZ_PATHS = ("/internal/authz/app", "/internal/authz/share")


def _proxy_secret() -> str:
    # Read per request: web may start before the proxy has written the file.
    path = os.environ.get("AGENTO_PROXY_SECRET_FILE", "/run/agento/proxy-secret")
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _from_proxy(given: str) -> bool:
    secret = _proxy_secret()
    return bool(secret) and hmac.compare_digest(given.encode(), secret.encode())


class Handler(BaseHTTPRequestHandler):
    def _route(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(200, b"ok")
        elif path in AUTHZ_PATHS:
            self._send(403 if _from_proxy(self.headers.get("X-Agento-Proxy-Auth", "")) else 401)
        else:
            self._send(404)

    do_GET = do_HEAD = do_POST = _route

    def _send(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # The proxy forwards the original query string (it may carry `cap` or a launch
    # exchange code), and the stdlib default logs the raw request line.
    def log_request(self, code="-", size="-") -> None:
        sys.stderr.write(f"{self.command} {urlsplit(self.path).path} {code}\n")

    def log_message(self, format, *args) -> None:
        sys.stderr.write(f"{getattr(self, 'command', None) or '-'} request error\n")


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
