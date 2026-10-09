"""The panel API, the launch redeem, and the authorization endpoints the proxy calls.

The allow/deny decision behind /internal/authz/app is E2's; the path it decides on is parsed
once, in app_path (E6). Shares never reach web: the artifacts server checks them (E6).

`web` shares db-net with `toolbox` and `cron`, so reachability proves nothing: a request is from
the proxy only if it carries the secret the proxy and web alone can read. Nothing on this
listener may trust an identity or forwarding header without that check.

FastAPI under uvicorn (DECISIONS.md 2026-10-09). SEC-12 is one ASGI middleware (`Sec12`) in
front of the router; handlers stay `api.Request` -> `api.Response` in the thread pool (PyMySQL blocks).
"""
from __future__ import annotations

import hmac
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl

import anyio
import uvicorn
from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.routing import BaseRoute, Match

from agento.framework.access import sessions

from . import api, rate_limit, security
from .streaming import StreamingResponse

AUTHZ_PATHS = ("/internal/authz/app",)
REDEEM_PATH = "/internal/launch/redeem"


def _from_proxy(given: str) -> bool:
    # Read per request: web may start before the proxy has written the file.
    try:
        secret = Path(os.environ.get("AGENTO_PROXY_SECRET_FILE", "/run/agento/proxy-secret")
                      ).read_text().strip()
    except OSError:
        return False
    return bool(secret) and hmac.compare_digest(given.encode(), secret.encode())


_WRITES = ("POST", "PUT", "PATCH", "DELETE")
MAX_JSON_BODY = 64 * 1024
_SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)
_UNAVAILABLE = api.Response(503, b"service unavailable")
# Streams sleep in their generator between polls, so they get their own thread budget: on the
# request pool 40 idle streams would stop every other request.
# ponytail: one thread per open stream; an async poll loop in conversation/src/stream.py past it.
MAX_STREAMS = 256
_STREAMS = anyio.CapacityLimiter(MAX_STREAMS)


def connect():
    from agento.framework.database_config import DatabaseConfig
    from agento.framework.db import get_connection

    return get_connection(DatabaseConfig.from_env())


def _too_many(retry) -> api.Response:
    return api.Response(429, {"error": "too many requests"}, [("Retry-After", str(retry))])


def _unavailable(exc: Exception) -> api.Response:
    # Fail closed: a limiter that cannot count is not a limiter (SEC-12).
    sys.stderr.write(f"rate limiter unavailable: {type(exc).__name__}\n")
    return _UNAVAILABLE


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


class Call:
    """One request: what SEC-12 learned about its caller, and its one DB connection."""

    def __init__(self, scope) -> None:
        self.command = scope["method"]
        self.path = scope["path"]
        self.query = scope["query_string"].decode("latin-1")
        self.headers = Headers(scope=scope)
        self.cookies = security.parse_cookies(self.headers.get("Cookie"))
        self.client_address = scope.get("client")
        self._conn = None
        self._buckets: list = []
        # The PROXY proving itself says nothing about WHO calls through it, so it has a field
        # of its own; nothing derived from an identity may read `_proxy_trusted`.
        self._authenticated, self._proxy_trusted, self._session = False, False, None
        self._launch_presented = False

    def _db(self):
        if self._conn is None:
            self._conn = connect()
        return self._conn

    def gate(self) -> api.Response | None:
        """SEC-12 in this order: private limiter, identity, stranger refusal; then OPTIONS."""
        refusal = self._limited()
        if refusal is None:
            self._prove_identity()
            refusal = self._refused_as_a_stranger()
        if refusal is None and self.command == "OPTIONS":
            # No preflight is ever answered: no credentialed CORS anywhere.
            refusal = api.error(405, "method not allowed")
        return refusal

    def _limited(self) -> api.Response | None:
        """Count the PRIVATE buckets (session, launch) of every request but /health.

        Mounted before every route, so a path that does not exist is counted too. The SHARED
        address bucket counts only what failed to authenticate (SEC-12), so one signed-in
        caller cannot lock every stranger behind its address out; `_refused_as_a_stranger`
        counts it.
        """
        if self.path in rate_limit.EXEMPT_PATHS:
            return None
        launch_token = next(iter(security.launch_cookies(self.cookies).values()), None)
        self._buckets = rate_limit.request_buckets(
            address=self.client_address[0] if self.client_address else None,
            session_token=self.cookies.get(security.SESSION_COOKIE),
            launch_token=launch_token,
        )
        self._launch_presented = launch_token is not None
        try:
            decision = rate_limit.check(self._db(), [b for b in self._buckets if b.private])
        except Exception as exc:
            return _unavailable(exc)
        return None if decision.allowed else _too_many(decision.retry_after)

    def _prove_identity(self) -> None:
        """Resolve WHO is calling, before any handler runs: one indexed session lookup, or the
        proxy's secret, with no side effect. The stranger refusal must land before a handler
        derives a password or redeems a single-use launch code (SEC-12).
        """
        if self.path in AUTHZ_PATHS:
            # The secret proves the PROXY, not the caller: as the caller's identity it skipped
            # the shared bucket, and rotating launch cookies got unlimited guesses.
            self._proxy_trusted = _from_proxy(self.headers.get("X-Agento-Proxy-Auth", ""))
            return
        # A route whose PURPOSE is to authenticate has no identity but its own outcome: a
        # session cookie on it would let a signed-in caller guess passwords or launch codes
        # past the address counter (SEC-12).
        route, _ = _match(self.command, self.path)
        if self.path == REDEEM_PATH or (route is not None and route.auth == "login"):
            return
        token = self.cookies.get(security.SESSION_COOKIE)
        if not token:
            return
        try:
            self._session = sessions.lookup_session(self._db(), token)
        except Exception as exc:                      # a lookup that cannot run proves nothing
            sys.stderr.write(f"session lookup failed: {type(exc).__name__}\n")
            return
        self._authenticated = self._session is not None

    def _refused_as_a_stranger(self) -> api.Response | None:
        """Count the SHARED buckets, the only place they are counted, for a caller that proved
        no identity, before any handler (SEC-12).

        A proxy-trusted subrequest comes once per request the proxy handles, so it spends
        nothing; it still reads the HOLD, the only stop on a brute force of a proxy-only route.
        """
        shared = [b for b in self._buckets if not b.private]
        if self._authenticated or not shared:
            return None
        try:
            if self._proxy_trusted:
                retry = rate_limit.held(self._db(), shared)
            else:
                decision = rate_limit.check(self._db(), shared)
                retry = decision.shared_refusal or (
                    0 if decision.allowed else decision.retry_after)
        except Exception as exc:
            return _unavailable(exc)
        return _too_many(retry) if retry else None

    def _api(self, route: api.Route, body: bytes | api.Response):
        origins = security.Origins.from_env()
        write = self.command in _WRITES
        if write and not security.write_allowed(self.headers, origins):
            return api.error(403, "forbidden")
        if isinstance(body, api.Response):
            return body
        req = api.Request(
            method=self.command, path=self.path, headers=self.headers, body=body,
            cookies=self.cookies, origins=origins,
            params=route.pattern.match(self.path).groupdict(),
            # keep_blank_values: `?after=` is present-but-empty, and the route answers 400.
            # Reversed before dict(): `Request.query` documents the FIRST of a repeat.
            query=dict(reversed(parse_qsl(self.query, keep_blank_values=True))),
        )
        if route.json_body:
            try:
                req.json = json.loads(body)
            except ValueError:
                return api.error(400, "invalid JSON")
        return self._run(route.handler, req, session=route.auth == "session", write=write)

    def _internal(self, handler, body: bytes):
        return self._run(handler, api.Request(
            method=self.command, path=self.path, headers=self.headers, body=body,
            cookies=self.cookies, origins=security.Origins.from_env()))

    def _run(self, handler, req: api.Request, *, session=False, write=False):
        try:
            req.conn = conn = self._db()
            if session:
                # Resolved once, in `_prove_identity`: a second lookup could answer otherwise.
                req.session = self._session
                if req.session is None:
                    return api.error(401, "not signed in")
                # Per user, not per token: signing in again does not reset the budget.
                identity = rate_limit.count_identity(conn, req.session.user.id)
                if not identity.allowed:
                    return _too_many(identity.retry_after)
                req.session_token = token = req.cookies.get(security.SESSION_COOKIE)
                if write and not sessions.csrf_valid(token, self.headers.get("X-CSRF-Token")):
                    return api.error(403, "forbidden")
            return handler(req)
        except Exception as exc:
            sys.stderr.write(f"{self.command} {self.path} failed: {type(exc).__name__}\n")
            return api.error(500, "internal error")

    def _failed_auth(self, status: int) -> bool:
        """Every 401/403 this listener sends feeds the hold, wherever it was decided (SEC-12).

        False when it could NOT be counted: the caller then gets 503, because an uncounted
        failure is a free guess. An authenticated caller's 403 is authorization, not counted.
        """
        if status not in (401, 403) or self._authenticated or not self._buckets:
            return True
        if self._proxy_trusted and not self._launch_presented:
            # The proxy asks for every request it forwards; with no launch cookie nothing was
            # guessed, and plain browsing must not place the hold.
            return True
        try:
            rate_limit.record_auth_failure(self._db(), self._buckets)
            return True
        except Exception as exc:
            sys.stderr.write(f"recording a failed authentication failed: {type(exc).__name__}\n")
            return False


async def _read_body(receive, headers, limit: int, json_body: bool) -> bytes | api.Response:
    try:
        length = int(headers.get("Content-Length") or 0)
    except ValueError:
        return api.error(400, "bad Content-Length")
    if length > limit:
        return api.error(413, "body too large")
    if json_body:
        ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return api.error(400, "Content-Type must be application/json")
    body = b""
    while True:                       # bounded too when the body is chunked, with no length
        message = await receive()
        body += message.get("body", b"")
        if len(body) > limit:
            return api.error(413, "body too large")
        if not message.get("more_body"):
            return body


def _raw(headers) -> list[tuple[bytes, bytes]]:
    return [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers]


async def _write(send, resp: api.Response) -> None:
    """A bytes body is plain text (/health, the authz answers); anything else is JSON."""
    if isinstance(resp.body, bytes):
        ctype, body = "text/plain", resp.body
    else:
        ctype, body = "application/json", b"" if resp.body is None else json.dumps(resp.body).encode()
    headers = [*resp.headers, ("Content-Type", ctype), ("Content-Length", str(len(body)))]
    await send({"type": "http.response.start", "status": resp.status, "headers": _raw(headers)})
    await send({"type": "http.response.body", "body": body})


async def _stream(receive, send, resp: StreamingResponse) -> None:
    """Write frames as they are produced; web/streaming.py has the contract.

    uvicorn drops writes to a closed socket, so a watcher on `http.disconnect` cancels the
    loop. The `finally` closes the generator once, with no `next()` in flight.
    """
    await send({"type": "http.response.start", "status": resp.status, "headers": _raw(resp.headers)})
    frames = iter(resp.frames)
    try:
        async with anyio.create_task_group() as tasks:
            async def watch() -> None:
                while (await receive())["type"] != "http.disconnect":
                    pass
                tasks.cancel_scope.cancel()

            tasks.start_soon(watch)
            while (chunk := await anyio.to_thread.run_sync(next, frames, None,
                                                           limiter=_STREAMS)) is not None:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b""})
            tasks.cancel_scope.cancel()
    except Exception as exc:
        # The headers went out: no 500 is left to send. Log why the body is truncated.
        sys.stderr.write(f"stream failed: {type(exc).__name__}\n")
    finally:
        getattr(resp.frames, "close", lambda: None)()   # a plain iterator has nothing to release


class RegexRoute(BaseRoute):
    """The router's one route: the built-in paths and `api.ROUTES`, read per request, so
    di.json routes keep their raw regex `(?P<id>...)` contract (CODE-5)."""

    def matches(self, scope):
        return (Match.FULL if scope["type"] == "http" else Match.NONE), {}

    async def handle(self, scope, receive, send) -> None:
        call: Call = scope["agento.call"]
        path = call.path
        if path == "/health":
            resp = api.Response(200, b"ok")
        elif path in AUTHZ_PATHS:
            if call._proxy_trusted:             # only the proxy may ask this question
                resp = await anyio.to_thread.run_sync(call._internal, api.authorize_app, b"")
            else:
                resp = api.Response(401, b"")
        elif path == REDEEM_PATH:
            body = (await _read_body(receive, call.headers, api.MAX_FORM_BODY, False)
                    if call.command == "POST" else api.error(405, "method not allowed"))
            resp = body if isinstance(body, api.Response) else await anyio.to_thread.run_sync(
                call._internal, api.redeem_launch, body)
        elif path.startswith("/api/"):
            route, known = _match(call.command, path)
            if route is None:
                resp = api.error(405 if known else 404, "method not allowed" if known else "not found")
            else:
                body = await _read_body(receive, call.headers, MAX_JSON_BODY, route.json_body)
                resp = await anyio.to_thread.run_sync(call._api, route, body)
        else:
            resp = api.Response(404, b"")
        if isinstance(resp, StreamingResponse):
            await _stream(receive, send, resp)
        else:
            await _write(send, resp)


class Sec12:
    """SEC-12 before the router: `Call.gate`, security headers, every 401/403 to `_failed_auth`
    (503 when it cannot count), a `METHOD path status` log without the query (SEC-6), and
    the request's DB connection closed at the end."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # The routes and the exempt and authz names match the undecoded path (CODE-5).
        scope["path"] = scope.get("raw_path", scope["path"].encode()).decode("latin-1")
        call = scope["agento.call"] = Call(scope)
        status, replaced = 0, False

        async def secured(message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message["headers"], *_raw(_SECURITY_HEADERS)]}
            await send(message)

        async def guarded(message) -> None:
            nonlocal status, replaced
            if replaced:
                return
            if message["type"] == "http.response.start":
                status = message["status"]
                if not await anyio.to_thread.run_sync(call._failed_auth, status):
                    status, replaced = 503, True
                    await _write(secured, _UNAVAILABLE)
                    return
            await secured(message)

        try:
            refusal = await anyio.to_thread.run_sync(call.gate)
            if refusal is not None:
                await _write(guarded, refusal)
            else:
                await self.app(scope, receive, guarded)
        finally:
            sys.stderr.write(f"{call.command} {call.path} {status}\n")
            if call._conn is not None:
                call._conn.close()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False,
              routes=[RegexRoute()])
app.add_middleware(Sec12)


def compose_routes() -> None:
    """Add every enabled module's routes to `api.ROUTES`, once at startup (`module:enable`
    restarts `web`)."""
    from agento.framework.bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
    from agento.framework.module_discovery import module_dirs_by_name

    from .routes_registry import load_module_routes

    etc = Path("/app/etc") if Path("/app/etc").is_dir() else Path.cwd() / "app" / "etc"
    # The shared discovery carries the extension mount and shadowing rules (PyPI routes too).
    api.ROUTES.extend(load_module_routes(etc, module_dirs_by_name(CORE_MODULES_DIR,
                                                                 USER_MODULES_DIR)))


# The query may carry `cap` or an exchange code: no access log, `Sec12` logs the path (SEC-6).
# No proxy headers: the limiter keys on the socket peer. A stop cuts open streams after 5 s
# (inside the compose grace); the browser resumes from `Last-Event-ID`.
UVICORN_FLAGS = {"log_config": None, "access_log": False, "server_header": False,
                 "proxy_headers": False, "timeout_graceful_shutdown": 5}


def main() -> None:
    compose_routes()
    uvicorn.run(app, host="0.0.0.0", port=8000, **UVICORN_FLAGS)
