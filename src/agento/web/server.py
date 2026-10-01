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
from typing import ClassVar
from urllib.parse import parse_qsl, urlsplit

from agento.framework.access import sessions

from . import api, rate_limit, security
from .streaming import StreamingResponse

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
    _conn = None
    _buckets: ClassVar[list] = []
    # Whether the caller proved an identity, and the session that proved it.
    # `_refused_as_a_stranger` reads them to decide who spends the shared bucket, BEFORE
    # any handler runs (§7.5, SEC-12).
    _authenticated = False
    # The PROXY proved itself, which says nothing about WHO is calling through it.
    _proxy_trusted = False
    _launch_presented = False
    _session = None

    def _db(self):
        """One connection per request, opened on first use and closed by `_route`."""
        if self._conn is None:
            self._conn = connect()
        return self._conn

    def _limited(self, path: str) -> bool:
        """Count this request. True when it must not be dispatched.

        Mounted here, before every branch below, so a route cannot escape the limiter by
        omission - a request to a path that does not exist is counted too. /health is the
        one exemption, and it is named in rate_limit.EXEMPT_PATHS rather than claimed by a
        route.
        """
        self._authenticated, self._proxy_trusted, self._session = False, False, None
        self._launch_presented = False
        if path in rate_limit.EXEMPT_PATHS:
            return False
        cookies = security.parse_cookies(self.headers.get("Cookie"))
        session_token = cookies.get(security.SESSION_COOKIE)
        launch_token = next(iter(security.launch_cookies(cookies).values()), None)
        self._buckets = rate_limit.request_buckets(
            address=self.client_address[0] if self.client_address else None,
            session_token=session_token,
            launch_token=launch_token,
        )
        self._launch_presented = launch_token is not None
        try:
            # PRIVATE buckets only - session, launch. They belong to one caller, so counting
            # them before the identity is resolved costs nobody else anything. The SHARED
            # (address) bucket is deliberately NOT counted here: SEC-12 says the address
            # limit counts only what failed to authenticate, "so one caller cannot throttle
            # the others' authorized traffic". Counting every request on it let one signed-in
            # caller flood the shared budget and lock every stranger behind that address -
            # another user's sign-in included - out of the panel. It is counted in
            # `_refused_as_a_stranger`, once we know the caller proved nothing.
            decision = rate_limit.check(self._db(), [b for b in self._buckets if b.private])
        except Exception as exc:
            # Fail closed: a limiter that cannot count is not a limiter (SEC-12).
            sys.stderr.write(f"rate limiter unavailable: {type(exc).__name__}\n")
            self._send(503, b"service unavailable")
            return True
        if not decision.allowed:
            self._reply(api.Response(429, {"error": "too many requests"},
                                     [("Retry-After", str(decision.retry_after))]))
            return True
        return False

    def _prove_identity(self, path: str) -> None:
        """Resolve WHO is calling, before anything runs on their behalf.

        Identity resolution is split from route execution on purpose. A shared bucket
        refuses only a caller that proved nothing, and the refusal has to land before the
        handler - otherwise a held address still gets its password derived on the sign-in
        route, and still gets its single-use launch code redeemed and committed, with the
        429 arriving only in place of the answer. Both are side effects, not just work.

        Cheap and side-effect-free by construction: one indexed session lookup, or the
        proxy's own secret. Nothing here consumes anything.

        The proxy's secret answers a different question - which hop, not which caller - so it
        lands in `_proxy_trusted`. Nothing derived from an identity may read that field.
        """
        if path in AUTHZ_PATHS:
            # The secret proves the PROXY, not the caller. Treating it as the caller's own
            # identity was a way through SEC-12: the shared bucket was neither consulted nor
            # told about the failure, so an attacker with rotating launch cookies got a fresh
            # private bucket per attempt and an unlimited number of free guesses.
            self._proxy_trusted = _from_proxy(self.headers.get("X-Agento-Proxy-Auth", ""))
            return
        if self._is_an_authentication_attempt(path):
            return
        token = security.parse_cookies(self.headers.get("Cookie")).get(security.SESSION_COOKIE)
        if not token:
            return
        try:
            self._session = sessions.lookup_session(self._db(), token)
        except Exception as exc:                      # a lookup that cannot run proves nothing
            sys.stderr.write(f"session lookup failed: {type(exc).__name__}\n")
            return
        self._authenticated = self._session is not None

    def _is_an_authentication_attempt(self, path: str) -> bool:
        """A route whose PURPOSE is to authenticate. Its own outcome is the only identity
        it has, so the session cookie it happens to carry proves nothing about it.

        Counting it as authenticated would hand a signed-in caller a free pass: they could
        guess another user's password, or spend forged launch codes, with the address
        counter silent and the shared refusal not applying to them (SEC-12).
        """
        if path == REDEEM_PATH:
            return True
        route, _ = _match(self.command, path)
        return route is not None and route.auth == "login"

    def _refused_as_a_stranger(self) -> bool:
        """Count the SHARED buckets and refuse - for a caller that proved no identity.

        This is where the address bucket is counted, and the only place: an authenticated
        request never reaches it, so authorized traffic cannot spend the budget that keeps
        strangers out (SEC-12). It still runs BEFORE any handler, so a held address gets no
        password derived on the sign-in route and no single-use launch code redeemed.

        A proxy-trusted subrequest is a third case: not a caller identity, but not a stranger
        to be charged either, since the proxy makes one per request it handles. It reads the
        hold and spends nothing.
        """
        if self._authenticated or not self._buckets:
            return False
        shared = [b for b in self._buckets if not b.private]
        if not shared:
            return False
        try:
            if self._proxy_trusted:
                # One subrequest per request the proxy handles: counting them on the shared
                # ceiling would spend the strangers' budget on everyone's ordinary traffic.
                # The HOLD still applies - it is placed by failures, and it is the only thing
                # that stops a brute force on a route only the proxy can reach (SEC-12).
                retry = rate_limit.held(self._db(), shared)
            else:
                decision = rate_limit.check(self._db(), shared)
                retry = decision.shared_refusal or (
                    0 if decision.allowed else decision.retry_after)
        except Exception as exc:
            # Fail closed, exactly as `_limited` does (SEC-12).
            sys.stderr.write(f"rate limiter unavailable: {type(exc).__name__}\n")
            self._send(503, b"service unavailable")
            return True
        if not retry:
            return False
        self._reply(api.Response(429, {"error": "too many requests"},
                                 [("Retry-After", str(retry))]))
        return True

    def _route(self) -> None:
        try:
            self._dispatch()
        finally:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _dispatch(self) -> None:
        path = urlsplit(self.path).path
        if self._limited(path):
            return
        self._prove_identity(path)
        if self._refused_as_a_stranger():
            return
        if self.command == "OPTIONS":
            # No preflight is ever answered: no credentialed CORS anywhere.
            self._reply(api.error(405, "method not allowed"))
        elif path == "/health":
            self._send(200, b"ok")
        elif path in AUTHZ_PATHS:
            if not self._proxy_trusted:         # only the proxy may ask this question
                self._send(401)
                return
            if path == "/internal/authz/app" and security.launch_cookies(
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
            # keep_blank_values: `?after=` is a present-but-empty parameter, and the route
            # answers 400 for it; dropped, it reads as absent and gets the default.
            # Reversed before dict(): `Request.query` documents the FIRST of a repeat.
            query=dict(reversed(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))),
        )
        if route.json_body:
            try:
                req.json = json.loads(body)
            except ValueError:
                self._reply(api.error(400, "invalid JSON"))
                return
        try:
            req.conn = conn = self._db()
            if route.auth == "session":
                # Resolved once, in `_prove_identity`, before the limiter's shared refusal
                # was applied - looking it up a second time here would be a second query
                # and a second chance for the two answers to differ.
                token = req.cookies.get(security.SESSION_COOKIE)
                req.session = self._session
                if req.session is None:
                    self._reply(api.error(401, "not signed in"))
                    return
                # Rotating the session token is a new session bucket but the same user
                # bucket, so the per-user budget cannot be evaded by signing in again.
                identity = rate_limit.count_identity(conn, req.session.user.id)
                if not identity.allowed:
                    self._reply(api.Response(429, {"error": "too many requests"},
                                             [("Retry-After", str(identity.retry_after))]))
                    return
                req.session_token = token
                if write and not sessions.csrf_valid(token, self.headers.get("X-CSRF-Token")):
                    self._reply(api.error(403, "forbidden"))
                    return
            self._reply(route.handler(req))
        except Exception as exc:
            sys.stderr.write(f"{self.command} {path} failed: {type(exc).__name__}\n")
            self._reply(api.error(500, "internal error"))

    def _internal(self, path: str, handler, body: bytes) -> None:
        req = api.Request(
            method=self.command, path=path, headers=self.headers, body=body,
            cookies=security.parse_cookies(self.headers.get("Cookie")), origins=security.Origins.from_env(),
        )
        try:
            req.conn = self._db()
            self._reply(handler(req))
        except Exception as exc:
            sys.stderr.write(f"{self.command} {path} failed: {type(exc).__name__}\n")
            self._reply(api.error(500, "internal error"))

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

    def _failed_auth(self, status: int) -> bool:
        """Every 401 this listener sends feeds the hold, wherever it was decided.

        False when the failure could NOT be counted: the caller then gets 503 instead of the
        401, because a limiter that cannot count a failed authentication is not a limiter,
        and answering the 401 anyway is an unbounded number of free guesses (SEC-12).

        One rule in one place: a dead session cookie, a failed login, a call without the
        proxy secret and a spent exchange code are all failed authentication, and counting
        only the ones a particular branch remembered to report is how the hold gets missed
        on exactly the path being brute-forced.
        """
        # 401 AND 403: SEC-12 names both, because a flood of random tokens is answered
        # with whichever the route reached for. An authenticated caller's 403 is an
        # authorization failure, not an authentication one, and is not counted.
        if status not in (401, 403) or self._authenticated or not self._buckets:
            return True
        if self._proxy_trusted and not self._launch_presented:
            # The proxy asks this question for every request it forwards, and a visitor with
            # no launch cookie is the ordinary answer to it - nothing was presented, so
            # nothing was guessed. Counting it would let plain browsing place the hold that
            # exists to stop guessing (SEC-12).
            return True
        try:
            rate_limit.record_auth_failure(self._db(), self._buckets)
            return True
        except Exception as exc:
            sys.stderr.write(f"recording a failed authentication failed: {type(exc).__name__}\n")
            return False

    def _reply(self, resp) -> None:
        if isinstance(resp, StreamingResponse):
            self._stream(resp)
            return
        if not self._failed_auth(resp.status):
            self._send(503, b"service unavailable")
            return
        body = b"" if resp.body is None else json.dumps(resp.body).encode()
        self.send_response(resp.status)
        for name, value in resp.headers:
            self.send_header(name, value)
        self._send_common("application/json", body)

    def _stream(self, resp: StreamingResponse) -> None:
        """Write frames as they are produced. See web/streaming.py for the contract.

        No `Content-Length`, so the end of the body is the end of the connection: this
        response is not keep-alive and says so. The `finally` closes the generator on every
        exit - that close is what runs the handler's own `finally` and releases its §7.3
        slot, and it must happen for a dropped client exactly as for a clean end.
        """
        self.close_connection = True
        self.send_response(resp.status)
        for name, value in (*_SECURITY_HEADERS, *resp.headers):
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for chunk in resp.frames:
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The reader left. Not an error, and there is no response left to send it.
            pass
        except Exception as exc:
            # The headers went out long ago: there is no 500 left to send. Cut the stream
            # and say so where the other failures are said, or the caller sees a truncated
            # body with no trace of why.
            sys.stderr.write(f"stream {self.path} failed: {type(exc).__name__}\n")
        finally:
            # Generators only. An iterator without `close()` has no `finally` to run, so
            # there is nothing to release either.
            close = getattr(resp.frames, "close", None)
            if close is not None:
                close()

    def _send(self, status: int, body: bytes = b"") -> None:
        # Same fail-closed rule as `_reply`: this is the path the AUTHZ 401 takes, which is
        # precisely the one a brute force runs through (SEC-12). No recursion - 503 is not
        # an authentication failure, so `_failed_auth` returns True for it.
        if not self._failed_auth(status):
            status, body = 503, b"service unavailable"
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


def compose_routes() -> None:
    """Add every enabled module's declared routes to the one dispatch table, once.

    At startup only: `web` never re-bootstraps, so a module's routes appear or disappear
    when `web` restarts - which `module:enable` already does.
    """
    from agento.framework.bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
    from agento.framework.module_discovery import module_dirs_by_name

    from .routes_registry import load_module_routes

    etc = Path("/app/etc") if Path("/app/etc").is_dir() else Path.cwd() / "app" / "etc"
    # The shared discovery, not a hand-written list of roots: it carries the container
    # extension mount and the shadowing rules with it, so an installed PyPI extension's
    # routes are served instead of answering 404.
    api.ROUTES.extend(load_module_routes(etc, module_dirs_by_name(CORE_MODULES_DIR,
                                                                 USER_MODULES_DIR)))


def main() -> None:
    compose_routes()
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
