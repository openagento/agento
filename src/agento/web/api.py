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

from agento.framework.access import accounts, launches, sessions
from agento.framework.access.passwords import dummy_verify

from . import app_path, security


@dataclass
class Request:
    method: str
    path: str
    headers: Any
    body: bytes
    cookies: dict[str, str]
    origins: security.Origins
    params: dict[str, str] = field(default_factory=dict)
    # First value per key, already decoded. A repeated key is a caller mistake, not a list:
    # every parameter this API takes is scalar.
    query: dict[str, str] = field(default_factory=dict)
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
    # Required on every route (D-ADMIN-ACL-2): an admin panel leaf the session's user must hold
    # (a tuple: any one of them), "self" (any signed-in user; the handler scopes), or
    # "anonymous" (auth="login" only). server._run checks it; compose_routes validates it.
    acl: str | tuple[str, ...] = field(kw_only=True)


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
    signed_in = sessions.sign_in(req.conn, username, password)
    if signed_in is None:
        THROTTLE.fail(username)
        return error(401, _INVALID)
    THROTTLE.reset(username)
    session, token = signed_in
    return Response(
        200,
        _session_json(req.conn, session, token),
        [("Set-Cookie", security.session_cookie(token, _seconds_until(session.expires_at)))],
    )


_DISPLAY_FIELDS = {"date_format": "locale/date_format", "timezone": "locale/timezone"}


def display_settings(conn) -> dict[str, str] | None:
    """The ``admin`` module's panel display settings at the default scope, or None when the
    module is disabled. The Config screen's resolver (ENV -> DB -> config.json); a value
    that is not one of the field's options gets the config.json default."""
    from agento.framework import module_status
    from agento.framework.admin import data
    from agento.framework.config_resolver import read_config_defaults

    # Per request, as _miniapps_enabled: get_module_schemas() is cached for the process.
    if not module_status.is_enabled("admin", module_status.read_module_status()):
        return None
    schema = next((m for m in data.get_module_schemas() if m.name == "admin"), None)
    if schema is None or schema.module_path is None:
        return None
    defaults = read_config_defaults(schema.module_path)
    fields = {f.field_name: f for f in data.get_resolved_fields(conn, "admin")}
    out = {}
    for key, name in _DISPLAY_FIELDS.items():
        f = fields[name]
        out[key] = f.value if f.value in {o["value"] for o in f.options or []} else defaults[name]
    return out


def _session_json(conn, session: sessions.Session, token: str) -> dict:
    return {"user": {**user_json(session.user), "acl": accounts.held_admin(conn, session.user.role)}, "csrf_token": sessions.csrf_token(token),
            "expires_at": _iso(session.expires_at), "display": display_settings(conn)}


def get_session(req: Request) -> Response:
    return Response(200, _session_json(req.conn, req.session, req.session_token))


def logout(req: Request) -> Response:
    sessions.revoke_session(req.conn, req.session.id)
    return Response(204, None, [("Set-Cookie", security.clear_cookie(security.SESSION_COOKIE))])


def _body(req: Request) -> dict:
    return req.json if isinstance(req.json, dict) else {}


def _positive_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _access_error(exc: accounts.AccessError) -> Response:
    msg = str(exc)
    if msg == "not allowed":
        return error(403, "forbidden")
    return error(404 if msg.endswith("not found") else 400, msg)


def _actor(req: Request) -> accounts.Actor:
    """The ``(id, role)`` the route check authorized; the writer re-checks it on the locked row."""
    return req.session.user.id, req.session.user.role


def agent_views(req: Request) -> Response:
    return Response(200, [
        {"id": v["id"], "code": v["code"], "label": v["label"], "workspace_id": v["workspace_id"]}
        for v in accounts.visible_agent_views(req.conn, req.session.user)
    ])


def _resolve_scope(req: Request, body: dict) -> tuple[int, int | None] | Response:
    """Exactly one of agent_view_id / workspace_id, reachable by the caller, else 404."""
    view_id, workspace_id = body.get("agent_view_id"), body.get("workspace_id")
    if (view_id is None) == (workspace_id is None):
        return error(400, "set exactly one of agent_view_id or workspace_id")
    if view_id is not None:
        if not _positive_int(view_id):
            return error(400, "agent_view_id must be a positive integer")
        with req.conn.cursor() as cur:
            cur.execute("SELECT workspace_id FROM agent_view WHERE id = %s", (view_id,))
            row = cur.fetchone()
        if not row:
            return error(404, "not found")
        workspace_id = row["workspace_id"]
    elif not _positive_int(workspace_id):
        return error(400, "workspace_id must be a positive integer")
    # 404, not 403: telling a user a scope exists is disclosure (PRD E2 §5).
    if not accounts.can_reach(req.conn, req.session.user, workspace_id=workspace_id, agent_view_id=view_id):
        return error(404, "not found")
    return workspace_id, view_id


def invoke(req: Request) -> Response:
    from .toolbox_client import invoke_tool

    body = _body(req)
    scope = _resolve_scope(req, body)
    if isinstance(scope, Response):
        return scope
    arguments = body.get("arguments", {})
    if not isinstance(arguments, dict):
        return error(400, "arguments must be an object")
    result = invoke_tool(req.conn, req.session, req.params["name"], arguments,
                         workspace_id=scope[0], agent_view_id=scope[1])
    return Response(result.status, result.body)


def admin_list_users(req: Request) -> Response:
    return Response(200, [user_json(u) for u in accounts.list_users(req.conn)])


def admin_create_user(req: Request) -> Response:
    body = _body(req)
    password = body.get("password")
    if password is not None and not isinstance(password, str):
        return error(400, "password must be a string")
    try:
        user = accounts.create_user(req.conn, body.get("username"), body.get("role"), password,
                                    actor=_actor(req))
    except accounts.AccessError as exc:
        return _access_error(exc)
    return Response(201, user_json(user))


def admin_update_user(req: Request) -> Response:
    body, user_id = _body(req), int(req.params["id"])
    if not {"role", "is_active", "password"} & body.keys():
        return error(400, "nothing to change")
    if "is_active" in body and not isinstance(body["is_active"], bool):
        return error(400, "is_active must be a boolean")
    if "password" in body and not isinstance(body["password"], str):
        return error(400, "password must be a string")
    if "role" in body and not isinstance(body["role"], str):
        return error(400, "role must be a string")
    try:
        accounts.update_user(req.conn, user_id, role=body.get("role"), active=body.get("is_active"),
                             password=body.get("password"), actor=_actor(req))
    except accounts.AccessError as exc:
        return _access_error(exc)
    return Response(200, user_json(accounts.get_user(req.conn, user_id)))


def _launch_json(launch: launches.Launch) -> dict:
    return {"launch_id": launch.id, "artifact_code": launch.artifact_code, "version_id": launch.version_id,
            "workspace_id": launch.workspace_id, "agent_view_id": launch.agent_view_id,
            "expires_at": _iso(launch.expires_at)}


def _miniapps_enabled() -> bool:
    """MOD-1: web is the only minter of a ``miniapp`` capability, so ``module:disable
    miniapps`` stops actions here at once (the module's own routes go when web restarts). The
    toolbox reads the same file too: its ``launch`` checker goes at the next toolbox start
    (toolbox/config-loader.js)."""
    from agento.framework import module_status

    return module_status.is_enabled("miniapps", module_status.read_module_status())


def launch_action(req: Request) -> Response:
    """POST /api/launches/<id>/actions/<tool>: a miniapp action, through the panel only."""
    from .toolbox_client import invoke_launch_action

    if not _miniapps_enabled():
        return error(404, "not found")
    arguments = _body(req).get("arguments", {})
    if not isinstance(arguments, dict):
        return error(400, "arguments must be an object")
    result = invoke_launch_action(req.conn, req.session, req.params["id"], req.params["tool"], arguments)
    return Response(result.status, result.body)


def list_launches(req: Request) -> Response:
    return Response(200, [_launch_json(x) for x in launches.list_launches(req.conn, req.session.user)])


def end_launch(req: Request) -> Response:
    if not launches.revoke_launch(req.conn, req.session.user, req.params["id"]):
        return error(404, "not found")
    return Response(204)


MAX_FORM_BODY = 4 * 1024


def redeem_launch(req: Request) -> Response:
    """POST /launch on the apps origin (proxied to /internal/launch/redeem).

    The exchange code is the credential, so the proxy secret is not required here: a stolen
    code is equally usable from anywhere. The form must come from the panel page: the exact
    panel Origin, which a page cannot forge. Sec-Fetch-Site is not checked, because panel and
    apps may be two sites (``panel.localhost`` and ``apps.localhost`` are) and it adds nothing
    to an exact Origin.
    """
    from urllib.parse import parse_qs

    if req.headers.get("Origin") != req.origins.panel:
        return error(403, "forbidden")
    ctype = (req.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/x-www-form-urlencoded":
        return error(400, "Content-Type must be application/x-www-form-urlencoded")
    form = parse_qs(req.body.decode("utf-8", "replace"), max_num_fields=4)
    launch_id, code = form.get("launch_id", [""])[0], form.get("code", [""])[0]
    if launch_id not in security.launch_cookies({security.LAUNCH_COOKIE_PREFIX + launch_id: ""}):
        return error(403, "forbidden")
    won = launches.redeem(req.conn, launch_id, code)
    if won is None:
        return error(403, "forbidden")
    launch, token = won
    presented = security.launch_cookies(req.cookies)
    live = launches.live_launch_ids(req.conn, [i for i in presented if i != launch.id])
    headers = [("Location", f"/a/{launch.artifact_code}/v/{launch.version_id}/"),
               ("Set-Cookie", security.launch_cookie(launch.id, token, _seconds_until(launch.expires_at)))]
    headers += [("Set-Cookie", security.clear_cookie(security.launch_cookie_name(i)))
                for i in presented if i != launch.id and i not in live]
    return Response(303, None, headers)


def authorize_app(req: Request) -> Response:
    """forward_auth for /a/<code>/v/<version>/ — called by the proxy only (the secret is checked first).

    Decides on the path it parsed from the raw X-Forwarded-Uri, and on allow tells the proxy
    to fetch exactly that path: one parse for the decision and the file (PRD E6 §6.2)."""
    parsed = app_path.parse_app_path(req.headers.get("X-Forwarded-Uri"))
    if parsed is None:
        return error(404, "not found")
    presented = security.launch_cookies(req.cookies)
    if not presented:
        return error(403, "forbidden")
    if launches.authorize_files(req.conn, list(presented.values()), parsed.artifact_code, parsed.version_id):
        return Response(200, None, [("X-Agento-Upstream-Path", parsed.upstream_path)])
    live = launches.live_launch_ids(req.conn, list(presented))
    return Response(403, {"error": "forbidden"},
                    [("Set-Cookie", security.clear_cookie(security.launch_cookie_name(i)))
                     for i in presented if i not in live])


def _r(method: str, pattern: str, handler, *, acl: str | tuple[str, ...], **kw) -> Route:
    return Route(method, re.compile(f"^{pattern}$"), handler, acl=acl, **kw)


ROUTES: list[Route] = [
    _r("POST", "/api/session", login, auth="login", json_body=True, acl="anonymous"),
    # "self": each handler scopes to the user's own rows or to the scope grants.
    _r("GET", "/api/session", get_session, acl="self"),
    _r("DELETE", "/api/session", logout, acl="self"),
    _r("GET", "/api/agent-views", agent_views, acl="self"),
    _r("POST", r"/api/tools/(?P<name>[a-z0-9_]+):invoke", invoke, json_body=True, acl="self"),
    _r("GET", "/api/launches", list_launches, acl="self"),
    _r("DELETE", r"/api/launches/(?P<id>[0-9a-f]{32})", end_launch, acl="self"),
    _r("POST", r"/api/launches/(?P<id>[0-9a-f]{32})/actions/(?P<tool>[a-z0-9_]{1,64})", launch_action,
       json_body=True, acl="self"),
    _r("GET", "/api/admin/users", admin_list_users, acl="admin.users"),
    _r("POST", "/api/admin/users", admin_create_user, json_body=True, acl="admin.users.create"),
    _r("PATCH", r"/api/admin/users/(?P<id>[0-9]{1,10})", admin_update_user, json_body=True, acl="admin.users.edit"),
]

# Last: admin_api imports the request types above and adds its routes to ROUTES. Its own module
# body does the add, so the routes are there whichever of the two modules is imported first.
from . import admin_api  # noqa: E402, F401
