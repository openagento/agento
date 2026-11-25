"""Panel API handlers (PRD E2 §4, §5). server.py applies the guards each Route names."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agento.framework import i18n
from agento.framework.access import accounts, launches, sessions
from agento.framework.access.passwords import dummy_verify

from . import app_path, security

logger = logging.getLogger(__name__)


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
    # > 0: the body is `multipart/form-data` with THIS byte cap instead of MAX_JSON_BODY.
    # A route that declares nothing is still JSON-only (SEC-9).
    multipart_bytes: int = 0
    revalidates: bool = False
    # Required on every route (D-ADMIN-ACL-2): an admin panel leaf the session's user must hold
    # (a tuple: any one of them), "self" (any signed-in user; the handler scopes), or
    # "anonymous" (auth="login" only). server._run checks it; compose_routes validates it.
    acl: str | tuple[str, ...] = field(kw_only=True)


def error(status: int, message: str, *, code: str | None = None, params: dict | None = None,
          extra: dict | None = None) -> Response:
    """A refusal body. ``code`` is what the panel translates; ``error`` is the English text for a log or a script.
    ``extra`` is module fields that go beside them; it cannot replace ``error``, ``code`` or ``params``."""
    body: dict[str, Any] = {**(extra or {}), "error": message}
    if code is not None:
        body["code"] = code
    if params is not None:
        body["params"] = params
    return Response(status, body)


def refusal(exc) -> Response:
    """The refusal of an exception that carries ``status``, and may carry ``code``, ``params`` and
    ``extra`` (module fields that go beside ``error``)."""
    return error(exc.status, str(exc), code=getattr(exc, "code", None), params=getattr(exc, "params", None),
                 extra=getattr(exc, "extra", None))


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _seconds_until(dt: datetime) -> int:
    return max(0, int((dt - datetime.now(UTC).replace(tzinfo=None)).total_seconds()))


def user_json(user: accounts.User) -> dict:
    return {"id": user.id, "username": user.username, "role": user.role, "is_active": user.is_active,
            "ui_locale": user.ui_locale}


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
        return error(400, "username and password are required", code="INVALID_REQUEST")
    # A name outside the grammar is never queried and never a throttle key: the `user`
    # table's collation is case-insensitive, so `Admin` would match `admin` under another key.
    if not accounts.USERNAME_RE.fullmatch(username):
        dummy_verify(password)
        return error(401, _INVALID, code="AUTH_INVALID_CREDENTIALS")
    if THROTTLE.blocked(username):
        return error(429, "too many failed logins, try later", code="AUTH_THROTTLED")
    signed_in = sessions.sign_in(req.conn, username, password)
    if signed_in is None:
        THROTTLE.fail(username)
        return error(401, _INVALID, code="AUTH_INVALID_CREDENTIALS")
    THROTTLE.reset(username)
    session, token = signed_in
    return Response(
        200,
        _session_json(req.conn, session, token),
        [("Set-Cookie", security.session_cookie(token, _seconds_until(session.expires_at)))],
    )


_DISPLAY_OWNER = "admin"
_DISPLAY_FIELDS = {"date_format": "locale/date_format", "timezone": "locale/timezone"}


@dataclass(frozen=True)
class DisplayField:
    path: str
    default: str
    options: frozenset[str]


_DISPLAY: dict[str, DisplayField] | None = None


def display_schema(manifests: Sequence) -> dict[str, DisplayField] | None:
    """What the files of the ``admin`` module say about the display settings: the options and
    the ``config.json`` default of each field. None when the module is not one of ``manifests``,
    or when a field is absent, is not a select, or has a default that is not one of its options.
    The files are read inside the module directory only (SEC-5). Read once, at start; this
    function raises for no content of these files."""
    from agento.framework.config_schema_options import field_options

    owner = next((m for m in manifests if m.name == _DISPLAY_OWNER), None)
    if owner is None:
        return None
    try:
        fields, defaults = (i18n.module_json(owner.path, name) for name in ("system.json", "config.json"))
        schema = {}
        for key, name in _DISPLAY_FIELDS.items():
            options = frozenset(o["value"] for o in field_options(fields[name]) if isinstance(o["value"], str))
            if not isinstance(defaults[name], str) or defaults[name] not in options:
                raise ValueError(name)
            schema[key] = DisplayField(f"{_DISPLAY_OWNER}/{name}", defaults[name], options)
        return schema
    except Exception as exc:
        logger.warning("panel display: the %s module files give no display settings (%s)",
                       _DISPLAY_OWNER, type(exc).__name__)
        return None


def install_display(schema: dict[str, DisplayField] | None) -> None:
    """Set the process snapshot: ``web`` does it once at start, with the catalogs."""
    global _DISPLAY
    _DISPLAY = schema


def display_settings(conn) -> dict[str, Any] | None:
    """The ``admin`` module's panel display settings at the default scope, or None when the
    module is disabled. ENV, then the default-scope DB row, then ``config.json``, as the Config
    screen resolves them; a value that is not one of the field's options gets the
    ``config.json`` default. The module files are the snapshot of the start: none is read here.
    Enablement is read per request, as ``_miniapps_enabled`` does."""
    from agento.framework import module_status, scoped_config
    from agento.framework.config_resolver import path_to_env_key

    if _DISPLAY is None or not module_status.is_enabled("admin", module_status.read_module_status()):
        return None
    stored = scoped_config.load_scoped_db_overrides(conn, scoped_config.Scope.DEFAULT, 0)
    out: dict[str, Any] = {}
    for key, setting in _DISPLAY.items():
        value = os.environ.get(path_to_env_key(setting.path))
        if value is None and setting.path in stored and not stored[setting.path][1]:
            value = stored[setting.path][0]
        known = value in setting.options
        out[key] = value if known else setting.default
        if key == "date_format":
            out["date_format_explicit"] = known
    return out


def locale_json(conn, stored: str | None) -> dict:
    """The language block of the session: what the panel shows, and what it may offer."""
    catalogs = i18n.current()
    instance = i18n.instance_language(conn)
    effective = i18n.effective(stored, instance, catalogs.languages)
    return {"effective": effective, "instance": instance,
            "available": [{"tag": tag, "name": name} for tag, name in catalogs.languages.items()],
            "version": catalogs.versions[effective]}


def _session_json(conn, session: sessions.Session, token: str) -> dict:
    return {"user": {**user_json(session.user), "acl": accounts.held_admin(conn, session.user.role)}, "csrf_token": sessions.csrf_token(token),
            "expires_at": _iso(session.expires_at), "display": display_settings(conn),
            "locale": locale_json(conn, session.user.ui_locale)}


def panel_bootstrap(req: Request) -> Response:
    """GET /api/panel/bootstrap: the sign-in screen's language and texts, for a caller with no session."""
    tag = i18n.instance_language(req.conn)
    login = i18n.current().login[tag]
    return Response(200, {"locale": tag, "version": login.version, "messages": {i18n.PANEL_NAMESPACE: login.messages}})


def panel_catalog(req: Request) -> Response:
    """GET /api/panel/i18n/<tag>: the served messages of one registered language."""
    catalogs, tag = i18n.current(), req.params["tag"]
    if tag not in catalogs.languages:
        return error(404, "not found", code="NOT_FOUND")
    etag = f'"{catalogs.versions[tag]}"'
    headers = [("ETag", etag)]
    if req.headers.get("If-None-Match") == etag:
        return Response(304, None, headers)
    return Response(200, {"locale": tag, "version": catalogs.versions[tag], "messages": catalogs.messages[tag],
                          "fallback": catalogs.fallback[tag]}, headers)


def set_profile_locale(req: Request) -> Response:
    """PATCH /api/profile/locale: the caller's own account, never an id from the body. With an
    ``If-Match`` header, the save needs it to be the ETag of the catalog the account will show."""
    body = req.json
    if not isinstance(body, dict) or set(body) != {"ui_locale"} or not isinstance(body["ui_locale"], str):
        return error(400, "the body must be {\"ui_locale\": \"<tag>\"}", code="INVALID_REQUEST")
    tag = body["ui_locale"]
    if tag not in i18n.current().languages:
        return error(400, "language not available", code="LOCALE_UNSUPPORTED", params={"ui_locale": tag})
    locale = locale_json(req.conn, tag)
    held = req.headers.get("If-Match")
    if held is not None and held != f'"{locale["version"]}"':
        return error(409, "the catalog changed", code="CATALOG_CHANGED")
    accounts.set_ui_locale(req.conn, req.session.user.id, tag)
    return Response(200, {"ui_locale": tag, "locale": locale})


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
        return error(403, "forbidden", code="FORBIDDEN")
    return error(404 if msg.endswith("not found") else 400, msg, code=exc.code, params=exc.params)


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
        return error(400, "set exactly one of agent_view_id or workspace_id", code="INVALID_REQUEST")
    if view_id is not None:
        if not _positive_int(view_id):
            return error(400, "agent_view_id must be a positive integer", code="INVALID_REQUEST")
        with req.conn.cursor() as cur:
            cur.execute("SELECT workspace_id FROM agent_view WHERE id = %s", (view_id,))
            row = cur.fetchone()
        if not row:
            return error(404, "not found", code="NOT_FOUND")
        workspace_id = row["workspace_id"]
    elif not _positive_int(workspace_id):
        return error(400, "workspace_id must be a positive integer", code="INVALID_REQUEST")
    # 404, not 403: telling a user a scope exists is disclosure (PRD E2 §5).
    if not accounts.can_reach(req.conn, req.session.user, workspace_id=workspace_id, agent_view_id=view_id):
        return error(404, "not found", code="NOT_FOUND")
    return workspace_id, view_id


def invoke(req: Request) -> Response:
    from .toolbox_client import invoke_tool

    body = _body(req)
    scope = _resolve_scope(req, body)
    if isinstance(scope, Response):
        return scope
    arguments = body.get("arguments", {})
    if not isinstance(arguments, dict):
        return error(400, "arguments must be an object", code="INVALID_REQUEST")
    result = invoke_tool(req.conn, req.session, req.params["name"], arguments,
                         workspace_id=scope[0], agent_view_id=scope[1])
    return Response(result.status, result.body)


def admin_list_users(req: Request) -> Response:
    return Response(200, [user_json(u) for u in accounts.list_users(req.conn)])


def admin_create_user(req: Request) -> Response:
    body = _body(req)
    password = body.get("password")
    if password is not None and not isinstance(password, str):
        return error(400, "password must be a string", code="INVALID_REQUEST")
    try:
        user = accounts.create_user(req.conn, body.get("username"), body.get("role"), password,
                                    actor=_actor(req))
    except accounts.AccessError as exc:
        return _access_error(exc)
    return Response(201, user_json(user))


def admin_update_user(req: Request) -> Response:
    body, user_id = _body(req), int(req.params["id"])
    if not {"role", "is_active", "password"} & body.keys():
        return error(400, "nothing to change", code="INVALID_REQUEST")
    if "is_active" in body and not isinstance(body["is_active"], bool):
        return error(400, "is_active must be a boolean", code="INVALID_REQUEST")
    if "password" in body and not isinstance(body["password"], str):
        return error(400, "password must be a string", code="INVALID_REQUEST")
    if "role" in body and not isinstance(body["role"], str):
        return error(400, "role must be a string", code="INVALID_REQUEST")
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
        return error(404, "not found", code="NOT_FOUND")
    arguments = _body(req).get("arguments", {})
    if not isinstance(arguments, dict):
        return error(400, "arguments must be an object", code="INVALID_REQUEST")
    result = invoke_launch_action(req.conn, req.session, req.params["id"], req.params["tool"], arguments)
    return Response(result.status, result.body)


def list_launches(req: Request) -> Response:
    return Response(200, [_launch_json(x) for x in launches.list_launches(req.conn, req.session.user)])


def end_launch(req: Request) -> Response:
    if not launches.revoke_launch(req.conn, req.session.user, req.params["id"]):
        return error(404, "not found", code="NOT_FOUND")
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
        return error(403, "forbidden", code="FORBIDDEN")
    ctype = (req.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/x-www-form-urlencoded":
        return error(400, "Content-Type must be application/x-www-form-urlencoded", code="INVALID_REQUEST")
    form = parse_qs(req.body.decode("utf-8", "replace"), max_num_fields=4)
    launch_id, code = form.get("launch_id", [""])[0], form.get("code", [""])[0]
    if launch_id not in security.launch_cookies({security.LAUNCH_COOKIE_PREFIX + launch_id: ""}):
        return error(403, "forbidden", code="FORBIDDEN")
    won = launches.redeem(req.conn, launch_id, code)
    if won is None:
        return error(403, "forbidden", code="FORBIDDEN")
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
        return error(404, "not found", code="NOT_FOUND")
    presented = security.launch_cookies(req.cookies)
    if not presented:
        return error(403, "forbidden", code="FORBIDDEN")
    if launches.authorize_files(req.conn, list(presented.values()), parsed.artifact_code, parsed.version_id):
        return Response(200, None, [("X-Agento-Upstream-Path", parsed.upstream_path)])
    live = launches.live_launch_ids(req.conn, list(presented))
    refused = error(403, "forbidden", code="FORBIDDEN")
    refused.headers += [("Set-Cookie", security.clear_cookie(security.launch_cookie_name(i)))
                        for i in presented if i not in live]
    return refused


def _r(method: str, pattern: str, handler, *, acl: str | tuple[str, ...], **kw) -> Route:
    return Route(method, re.compile(f"^{pattern}$"), handler, acl=acl, **kw)


ROUTES: list[Route] = [
    _r("POST", "/api/session", login, auth="login", json_body=True, acl="anonymous"),
    # "self": each handler scopes to the user's own rows or to the scope grants.
    _r("GET", "/api/session", get_session, acl="self"),
    _r("DELETE", "/api/session", logout, acl="self"),
    _r("GET", "/api/panel/bootstrap", panel_bootstrap, auth="login", acl="anonymous"),
    _r("GET", r"/api/panel/i18n/(?P<tag>[A-Za-z0-9-]{2,35})", panel_catalog, acl="self", revalidates=True),
    _r("PATCH", "/api/profile/locale", set_profile_locale, json_body=True, acl="self"),
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
