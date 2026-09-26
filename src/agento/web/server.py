"""The panel API, the launch redeem, and the authorization endpoints the proxy calls.

The allow/deny decision behind /internal/authz/app is E2's; /internal/authz/share is E6's
and until then denies every subrequest.

`web` shares agento-net with `sandbox`, so reachability proves nothing: a request is from
the proxy only if it carries the secret the proxy and web alone can read. Nothing on this
listener may trust an identity or forwarding header without that check.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from agento.framework.access import sessions

from . import api, security

AUTHZ_PATHS = ("/internal/authz/app", "/internal/authz/share")
REDEEM_PATH = "/internal/launch/redeem"


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


_WRITES = ("POST", "PUT", "PATCH", "DELETE")
MAX_JSON_BODY = 64 * 1024
_SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)


def connect():
    from agento.framework.database_config import DatabaseConfig
    from agento.framework.db import get_connection

    return get_connection(DatabaseConfig.from_env())


def _match(method: str, path: str) -> tuple[api.Route | None, bool]:
    """(route, path_known): a known path with another method answers 405."""
    known = False
    for route in api.ROUTES:
        m = route.pattern.match(path)
        if m:
            known = True
            if route.method == method:
                return route, True
    return None, known


class Handler(BaseHTTPRequestHandler):
    def _route(self) -> None:
        path = urlsplit(self.path).path
        if self.command == "OPTIONS":
            # No preflight is ever answered: no credentialed CORS anywhere.
            self._reply(api.error(405, "method not allowed"))
        elif path == "/health":
            self._send(200, b"ok")
        elif path in AUTHZ_PATHS:
            if not _from_proxy(self.headers.get("X-Agento-Proxy-Auth", "")):
                self._send(401)
            elif path == "/internal/authz/app" and security.launch_cookies(
                    security.parse_cookies(self.headers.get("Cookie"))):
                self._internal(path, api.authorize_app, b"")
            else:
                self._send(403)
        elif path == REDEEM_PATH:
            if self.command != "POST":
                self._reply(api.error(405, "method not allowed"))
                return
            body = self._read_body(None, api.MAX_FORM_BODY)
            if isinstance(body, api.Response):
                self._reply(body)
            else:
                self._internal(path, api.redeem_launch, body)
        elif path.startswith("/api/"):
            self._api(path)
        else:
            self._send(404)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _route

    def _api(self, path: str) -> None:
        route, known = _match(self.command, path)
        if route is None:
            self._reply(api.error(405 if known else 404, "method not allowed" if known else "not found"))
            return
        origins = security.Origins.from_env()
        write = self.command in _WRITES
        if write and not security.write_allowed(self.headers, origins):
            self._reply(api.error(403, "forbidden"))
            return
        body = self._read_body(route, MAX_JSON_BODY)
        if isinstance(body, api.Response):
            self._reply(body)
            return
        req = api.Request(
            method=self.command, path=path, headers=self.headers, body=body,
            cookies=security.parse_cookies(self.headers.get("Cookie")), origins=origins,
            params=route.pattern.match(path).groupdict(),
        )
        if route.json_body:
            try:
                req.json = json.loads(body)
            except ValueError:
                self._reply(api.error(400, "invalid JSON"))
                return
        conn = None
        try:
            conn = connect()
            req.conn = conn
            if route.auth == "session":
                token = req.cookies.get(security.SESSION_COOKIE)
                req.session = sessions.lookup_session(conn, token)
                if req.session is None:
                    self._reply(api.error(401, "not signed in"))
                    return
                req.session_token = token
                if write and not sessions.csrf_valid(token, self.headers.get("X-CSRF-Token")):
                    self._reply(api.error(403, "forbidden"))
                    return
            self._reply(route.handler(req))
        except Exception as exc:
            sys.stderr.write(f"{self.command} {path} failed: {type(exc).__name__}\n")
            self._reply(api.error(500, "internal error"))
        finally:
            if conn is not None:
                conn.close()

    def _internal(self, path: str, handler, body: bytes) -> None:
        req = api.Request(
            method=self.command, path=path, headers=self.headers, body=body,
            cookies=security.parse_cookies(self.headers.get("Cookie")), origins=security.Origins.from_env(),
        )
        conn = None
        try:
            conn = connect()
            req.conn = conn
            self._reply(handler(req))
        except Exception as exc:
            sys.stderr.write(f"{self.command} {path} failed: {type(exc).__name__}\n")
            self._reply(api.error(500, "internal error"))
        finally:
            if conn is not None:
                conn.close()

    def _read_body(self, route: api.Route | None, limit: int) -> bytes | api.Response:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return api.error(400, "bad Content-Length")
        if length > limit:
            return api.error(413, "body too large")
        if route is not None and route.json_body:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return api.error(400, "Content-Type must be application/json")
        return self.rfile.read(length) if length > 0 else b""

    def _reply(self, resp: api.Response) -> None:
        body = b"" if resp.body is None else json.dumps(resp.body).encode()
        self.send_response(resp.status)
        for name, value in resp.headers:
            self.send_header(name, value)
        self._send_common("application/json", body)

    def _send(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self._send_common("text/plain", body)

    def _send_common(self, content_type: str, body: bytes) -> None:
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
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
