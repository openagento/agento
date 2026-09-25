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
    signed_in = sessions.sign_in(req.conn, username, password)
    if signed_in is None:
        THROTTLE.fail(username)
        return error(401, _INVALID)
    THROTTLE.reset(username)
    session, token = signed_in
    user = session.user
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


def _body(req: Request) -> dict:
    return req.json if isinstance(req.json, dict) else {}


def _positive_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _access_error(exc: accounts.AccessError) -> Response:
    msg = str(exc)
    if msg == "not allowed":
        return error(403, "forbidden")
    return error(404 if msg.endswith("not found") else 400, msg)


def _forbidden_unless(req: Request, operation: str) -> Response | None:
    # The role comes from this request's session lookup (the DB), never from the cookie.
    return None if accounts.may(req.session.user, operation) else error(403, "forbidden")


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
    return _forbidden_unless(req, "users.manage") or Response(
        200, [user_json(u) for u in accounts.list_users(req.conn)])


def admin_create_user(req: Request) -> Response:
    if denied := _forbidden_unless(req, "users.manage"):
        return denied
    body = _body(req)
    password = body.get("password")
    if password is not None and not isinstance(password, str):
        return error(400, "password must be a string")
    try:
        user = accounts.create_user(req.conn, body.get("username"), body.get("role"), password,
                                    actor_id=req.session.user.id)
    except accounts.AccessError as exc:
        return _access_error(exc)
    return Response(201, user_json(user))


def admin_update_user(req: Request) -> Response:
    if denied := _forbidden_unless(req, "users.manage"):
        return denied
    body, user_id, actor = _body(req), int(req.params["id"]), req.session.user.id
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
                             password=body.get("password"), actor_id=actor)
    except accounts.AccessError as exc:
        return _access_error(exc)
    return Response(200, user_json(accounts.get_user(req.conn, user_id)))


def _grant_json(g: dict) -> dict:
    return {**g, "created_at": _iso(g["created_at"]) if g.get("created_at") else None}


def admin_list_grants(req: Request) -> Response:
    return _forbidden_unless(req, "grants.manage") or Response(
        200, [_grant_json(g) for g in accounts.list_grants(req.conn)])


def admin_add_grant(req: Request) -> Response:
    if denied := _forbidden_unless(req, "grants.manage"):
        return denied
    body = _body(req)
    for key in ("workspace_id", "agent_view_id"):
        if body.get(key) is not None and not _positive_int(body[key]):
            return error(400, f"{key} must be a positive integer")
    try:
        grant_id = accounts.add_grant(
            req.conn, body.get("role"), body.get("kind"), body.get("name"),
            workspace_id=body.get("workspace_id"), agent_view_id=body.get("agent_view_id"),
            actor_id=req.session.user.id,
        )
    except accounts.AccessError as exc:
        return _access_error(exc)
    return Response(201, {"id": grant_id})


def admin_remove_grant(req: Request) -> Response:
    if denied := _forbidden_unless(req, "grants.manage"):
        return denied
    try:
        accounts.remove_grant(req.conn, int(req.params["id"]), actor_id=req.session.user.id)
    except accounts.AccessError as exc:
        return _access_error(exc)
    return Response(204)


_SCOPES = ("default", "workspace", "agent_view")


def admin_set_config(req: Request) -> Response:
    from agento.framework.config_write import ConfigWriteError, save_config

    if denied := _forbidden_unless(req, "config.write"):
        return denied
    body = _body(req)
    scope, scope_id = body.get("scope", "default"), body.get("scope_id", 0)
    if scope not in _SCOPES:
        return error(400, f"scope must be one of {', '.join(_SCOPES)}")
    if scope == "default":
        scope_id = 0
    elif not _positive_int(scope_id):
        return error(400, "scope_id must be a positive integer")
    try:
        # web holds no encryption key: allow_secret=False writes only a provably plain field.
        _encrypted, reset = save_config(
            req.conn, body.get("path"), body.get("value"), scope=scope, scope_id=scope_id,
            allow_secret=False, actor_id=req.session.user.id,
        )
    except ConfigWriteError as exc:
        return error(403, "forbidden") if str(exc) == "not allowed" else error(400, str(exc))
    # No admin route returns a config value; a repaired dependent is named, not shown.
    return Response(200, {"path": body.get("path"), "reset": [p for p, _v in reset]})


_ARTIFACT_CODE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_VERSION_ID = re.compile(r"^v-[0-9]{8}-[0-9]{6}-[a-z0-9]{4}$")
_TOOLBOX_DOWN = error(503, "toolbox unavailable")


def _launch_json(launch: launches.Launch) -> dict:
    return {"launch_id": launch.id, "artifact_code": launch.artifact_code, "version_id": launch.version_id,
            "workspace_id": launch.workspace_id, "agent_view_id": launch.agent_view_id,
            "expires_at": _iso(launch.expires_at)}


def _current_version(req: Request, code: str, workspace_id: int, view_id: int) -> str | Response:
    """`current` resolves at launch time through the toolbox; the apps origin has no `current` route."""
    import json

    from .toolbox_client import invoke_tool

    result = invoke_tool(req.conn, req.session, "versioned_artifact_get_current", {"artifact_code": code},
                         workspace_id=workspace_id, agent_view_id=view_id)
    if result.status >= 500:
        return _TOOLBOX_DOWN
    if not result.body.get("ok"):
        # Tool not granted or not enabled in this scope: the user cannot launch here.
        return error(404, "not found")
    try:
        payload = json.loads(result.body["result"]["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        return _TOOLBOX_DOWN
    version = payload.get("current_version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not _VERSION_ID.fullmatch(version):
        return error(409, "artifact has no published version")
    return version


def create_launch(req: Request) -> Response:
    body = _body(req)
    if body.get("agent_view_id") is None or body.get("workspace_id") is not None:
        return error(400, "agent_view_id is required")
    code = body.get("artifact_code")
    if not isinstance(code, str) or not _ARTIFACT_CODE.fullmatch(code):
        return error(400, "artifact_code must match ^[a-z0-9][a-z0-9-]{0,63}$")
    scope = _resolve_scope(req, body)
    if isinstance(scope, Response):
        return scope
    workspace_id, view_id = scope
    user = req.session.user
    if not accounts.has_operation(req.conn, user.role, "artifact.launch", workspace_id, view_id):
        return error(404, "not found")
    version = _current_version(req, code, workspace_id, view_id)
    if isinstance(version, Response):
        return version
    try:
        launch, exchange_code = launches.create_launch(req.conn, user, workspace_id=workspace_id,
                                                       agent_view_id=view_id, artifact_code=code, version_id=version)
    except launches.AccessConfigError:
        return error(503, "launches are not configured")
    except accounts.AccessError:
        return error(404, "not found")
    # The panel POSTs `fields` as a form to `url`: no URL ever carries the exchange code.
    return Response(201, {**_launch_json(launch),
                          "redeem": {"url": f"{req.origins.apps}/launch",
                                     "fields": {"launch_id": launch.id, "code": exchange_code}}})


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
    code is equally usable from anywhere. The form must come from the panel page.
    """
    from urllib.parse import parse_qs

    site = req.headers.get("Sec-Fetch-Site")
    if req.headers.get("Origin") != req.origins.panel or site not in (None, "same-site", "same-origin"):
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
    """forward_auth for /a/<code>/v/<version>/ — called by the proxy only (the secret is checked first)."""
    code, version = req.headers.get("X-Agento-Artifact-Code"), req.headers.get("X-Agento-Version-Id")
    if not code or not version or not _ARTIFACT_CODE.fullmatch(code) or not _VERSION_ID.fullmatch(version):
        return error(403, "forbidden")
    presented = security.launch_cookies(req.cookies)
    if launches.authorize_files(req.conn, list(presented.values()), code, version):
        return Response(200)
    live = launches.live_launch_ids(req.conn, list(presented))
    return Response(403, {"error": "forbidden"},
                    [("Set-Cookie", security.clear_cookie(security.launch_cookie_name(i)))
                     for i in presented if i not in live])


def _r(method: str, pattern: str, handler, **kw) -> Route:
    return Route(method, re.compile(f"^{pattern}$"), handler, **kw)


ROUTES: list[Route] = [
    _r("POST", "/api/session", login, auth="login", json_body=True),
    _r("GET", "/api/session", get_session),
    _r("DELETE", "/api/session", logout),
    _r("GET", "/api/agent-views", agent_views),
    _r("POST", r"/api/tools/(?P<name>[a-z0-9_]+):invoke", invoke, json_body=True),
    _r("POST", "/api/launches", create_launch, json_body=True),
    _r("GET", "/api/launches", list_launches),
    _r("DELETE", r"/api/launches/(?P<id>[0-9a-f]{32})", end_launch),
    _r("GET", "/api/admin/users", admin_list_users),
    _r("POST", "/api/admin/users", admin_create_user, json_body=True),
    _r("PATCH", r"/api/admin/users/(?P<id>[0-9]{1,10})", admin_update_user, json_body=True),
    _r("GET", "/api/admin/grants", admin_list_grants),
    _r("POST", "/api/admin/grants", admin_add_grant, json_body=True),
    _r("DELETE", r"/api/admin/grants/(?P<id>[0-9]{1,19})", admin_remove_grant),
    _r("PUT", "/api/admin/config", admin_set_config, json_body=True),
]
