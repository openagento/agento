"""Panel API handlers (PRD E2 §4, §5). server.py applies the guards each Route names."""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agento.framework.access import accounts, sessions
from agento.framework.access.passwords import dummy_verify

from . import security


@dataclass
class Request:
    method: str
    path: str
    headers: Any
    body: bytes
    cookies: dict[str, str]
    origins: security.Origins
    params: dict[str, str] = field(default_factory=dict)
    conn: Any = None
    session: sessions.Session | None = None
    session_token: str | None = None
    json: Any = None


@dataclass
class Response:
    status: int
    body: Any = None
    headers: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern
    handler: Callable[[Request], Response]
    # "session": a live panel session (and, for a write, the CSRF token); "login": no session.
    auth: str = "session"
    json_body: bool = False


def error(status: int, message: str) -> Response:
    return Response(status, {"error": message})


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _seconds_until(dt: datetime) -> int:
    return max(0, int((dt - datetime.now(UTC).replace(tzinfo=None)).total_seconds()))


def user_json(user: accounts.User) -> dict:
    return {"id": user.id, "username": user.username, "role": user.role, "is_active": user.is_active}


class LoginThrottle:
    """Failed logins per username in a sliding window."""

    # ponytail: per-process dict bounded to MAX_KEYS (oldest dropped); a DB-backed throttle
    # when web runs more than one replica.
    LIMIT, WINDOW, MAX_KEYS = 10, 15 * 60, 10_000

    def __init__(self) -> None:
        self._fails: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _recent(self, username: str, now: float) -> list[float]:
        return [t for t in self._fails.get(username, []) if now - t < self.WINDOW]

    def blocked(self, username: str) -> bool:
        with self._lock:
            return len(self._recent(username, time.monotonic())) >= self.LIMIT

    def fail(self, username: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._fails[username] = [*self._recent(username, now), now]
            self._fails.move_to_end(username)
            while len(self._fails) > self.MAX_KEYS:
                self._fails.popitem(last=False)

    def reset(self, username: str) -> None:
        with self._lock:
            self._fails.pop(username, None)

    def clear(self) -> None:
        with self._lock:
            self._fails.clear()


THROTTLE = LoginThrottle()
_INVALID = "invalid credentials"


def login(req: Request) -> Response:
    body = req.json if isinstance(req.json, dict) else {}
    username, password = body.get("username"), body.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return error(400, "username and password are required")
    # A name outside the grammar is never queried and never a throttle key: the `user`
    # table's collation is case-insensitive, so `Admin` would match `admin` under another key.
    if not accounts.USERNAME_RE.fullmatch(username):
        dummy_verify(password)
        return error(401, _INVALID)
    if THROTTLE.blocked(username):
        return error(429, "too many failed logins, try later")
    user = accounts.authenticate(req.conn, username, password)
    if user is None:
        THROTTLE.fail(username)
        return error(401, _INVALID)
    THROTTLE.reset(username)
    session, token = sessions.create_session(req.conn, user)
    return Response(
        200,
        {"user": user_json(user), "csrf_token": sessions.csrf_token(token), "expires_at": _iso(session.expires_at)},
        [("Set-Cookie", security.session_cookie(token, _seconds_until(session.expires_at)))],
    )


def get_session(req: Request) -> Response:
    return Response(200, {
        "user": user_json(req.session.user),
        "csrf_token": sessions.csrf_token(req.session_token),
        "expires_at": _iso(req.session.expires_at),
    })


def logout(req: Request) -> Response:
    sessions.revoke_session(req.conn, req.session.id)
    return Response(204, None, [("Set-Cookie", security.clear_cookie(security.SESSION_COOKIE))])


def _r(method: str, pattern: str, handler, **kw) -> Route:
    return Route(method, re.compile(f"^{pattern}$"), handler, **kw)


ROUTES: list[Route] = [
    _r("POST", "/api/session", login, auth="login", json_body=True),
    _r("GET", "/api/session", get_session),
    _r("DELETE", "/api/session", logout),
]
