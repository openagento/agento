"""The miniapps REST surface under ``/api/miniapps/`` (D-MINIAPP-3).

Every route is ``acl: "self"``: the rights are per app (``rights.py``), so the handler checks
them, as the conversation routes check thread ownership. No ``view`` of the app is 404 (telling
a user that an app exists is disclosure); ``view`` without the right is 403. A view-scoped check
(``artifact.launch``) answers 404 as before: a scope the user cannot use is not disclosed.
"""
from __future__ import annotations

from agento.framework.access import accounts, launches
from agento.modules.versioned_artifacts.src import admin as va_admin
from agento.web import api, app_path
from agento.web.api import Request, Response, error

from . import activation, rights, settings

_NOT_FOUND = error(404, "not found")
_FORBIDDEN = error(403, "forbidden")


def _user(req: Request) -> accounts.User:
    assert req.session is not None  # every module route is auth='session'
    return req.session.user


def _run(fn) -> Response:
    try:
        return fn()
    except activation.ActivationError as exc:
        return Response(exc.status, {"error": str(exc), **exc.extra})
    except va_admin.AdminError as exc:
        return Response(exc.status, {"error": str(exc), **exc.extra})
    except launches.RetentionBusy:
        return error(503, "artifact busy, try again")


def _may_approve_in(req: Request, view_id: int) -> tuple[int, int] | Response:
    """The view's ``(workspace_id, view_id)`` when the user may approve or preview there
    (admin, or ``artifact.launch`` in the view), else 404."""
    scope = api._resolve_scope(req, {"agent_view_id": view_id})
    if isinstance(scope, Response):
        return scope
    user = _user(req)
    if not (rights.is_admin(user) or accounts.has_operation(req.conn, user.role, "artifact.launch", *scope)):
        return _NOT_FOUND
    return scope


def _app(req: Request, code: str, need: str) -> rights.Rights | Response:
    """The user's rights on the app, when they hold ``need``: no view 404, else 403."""
    if not app_path.ARTIFACT_CODE_RE.fullmatch(code):
        return _NOT_FOUND
    r = rights.rights(req.conn, _user(req), code)
    if not r.view:
        return _NOT_FOUND
    return r if getattr(r, need) else _FORBIDDEN


def _approval_target(req: Request) -> tuple[str, int] | Response:
    code, view_id = req.params["code"], int(req.params["view"])
    held = _app(req, code, "edit")
    if isinstance(held, Response):
        return held
    scope = _may_approve_in(req, view_id)
    return scope if isinstance(scope, Response) else (code, view_id)


def approve(req: Request) -> Response:
    """POST /api/miniapps/apps/<code>/views/<view>/approval {expected_version_id, actions?}."""
    target = _approval_target(req)
    if isinstance(target, Response):
        return target
    body = api._body(req)
    expected, actions = body.get("expected_version_id"), body.get("actions")
    if not isinstance(expected, str) or not app_path.VERSION_ID_RE.fullmatch(expected):
        return error(400, "expected_version_id is required")
    if actions is not None and (not isinstance(actions, list) or not all(isinstance(a, str) for a in actions)):
        return error(400, "actions must be a list of tool names")
    return _run(lambda: Response(200, activation.approve(req.conn, _user(req), *target, expected, actions)))


def revoke(req: Request) -> Response:
    """DELETE /api/miniapps/apps/<code>/views/<view>/approval."""
    target = _approval_target(req)
    if isinstance(target, Response):
        return target
    return Response(204) if activation.revoke(req.conn, _user(req), *target) else _NOT_FOUND


def approval(req: Request) -> Response:
    """GET /api/miniapps/apps/<code>/views/<view>/approval: the view's approval and ``current``."""
    target = _approval_target(req)
    if isinstance(target, Response):
        return target
    return _run(lambda: Response(200, activation.approval(req.conn, _user(req), *target)))


def _approve_views(req: Request) -> list[dict]:
    user = _user(req)
    if rights.is_admin(user):
        return accounts.visible_agent_views(req.conn, user)
    return accounts.operation_views(req.conn, user.role, "artifact.launch")


def approvals(req: Request) -> Response:
    """GET /api/miniapps/apps/<code>/approvals: the app's approvals in the views where the user may approve."""
    held = _app(req, req.params["code"], "edit")
    if isinstance(held, Response):
        return held
    return Response(200, activation.approvals(req.conn, req.params["code"], [v["id"] for v in _approve_views(req)]))


def launch_views(req: Request) -> Response:
    """GET /api/miniapps/agent-views: the views where the user may approve and preview."""
    return Response(200, [{k: v[k] for k in ("id", "code", "label", "workspace_id")} for v in _approve_views(req)])


def catalogue(req: Request) -> Response:
    """GET /api/miniapps/apps: every app the user may view, with its rights and views. The panel probe too."""
    return _run(lambda: Response(200, activation.catalogue(req.conn, _user(req))))


def _launch(req: Request, *, preview: bool) -> Response:
    body = api._body(req)
    view_id, code = body.get("agent_view_id"), body.get("artifact_code")
    if not api._positive_int(view_id) or body.get("workspace_id") is not None:
        return error(400, "agent_view_id is required")
    if not isinstance(code, str) or not app_path.ARTIFACT_CODE_RE.fullmatch(code):
        return error(400, "artifact_code must match ^[a-z0-9][a-z0-9-]{0,63}$")
    if "preview" in body:
        return error(400, "preview is a route of its own: POST /api/miniapps/previews")
    user = _user(req)
    scope = _may_approve_in(req, view_id) if preview else api._resolve_scope(req, {"agent_view_id": view_id})
    if isinstance(scope, Response):
        return scope

    def authorize(user: accounts.User = user) -> None:
        # Run first, and again inside the launch's locked transaction with the stored role: a
        # member removed or a role changed in between gets no launch (D-MINIAPP-3).
        held = rights.rights(req.conn, user, code)
        if not held.view:
            raise activation.ActivationError(404, "not found")
        if not (held.edit if preview else rights.can_execute(req.conn, user, code, view_id)):
            raise activation.ActivationError(403, "forbidden")

    def go() -> Response:
        authorize()
        try:
            launch, exchange_code = activation.launch(req.conn, user, code, *scope, preview, authorize=authorize)
        except launches.AccessConfigError:
            return error(503, "launches are not configured")
        except accounts.AccessError:
            return _NOT_FOUND
        # The panel POSTs `fields` as a form to `url`: no URL ever carries the exchange code.
        return Response(201, {**api._launch_json(launch),
                              "redeem": {"url": f"{req.origins.apps}/launch",
                                         "fields": {"launch_id": launch.id, "code": exchange_code}}})

    return _run(go)


def create_launch(req: Request) -> Response:
    """POST /api/miniapps/launches {agent_view_id, artifact_code}: the approved version (``execute``)."""
    return _launch(req, preview=False)


def create_preview(req: Request) -> Response:
    """POST /api/miniapps/previews {agent_view_id, artifact_code}: ``current`` with no actions,
    approved or not (``edit``, and admin or ``artifact.launch`` in the view)."""
    return _launch(req, preview=True)


def app(req: Request) -> Response:
    """GET /api/miniapps/apps/<code>: settings and rights; the members only with ``edit``."""
    code = req.params["code"]
    held = _app(req, code, "view")
    if isinstance(held, Response):
        return held
    return Response(200, {"artifact_code": code, **settings.settings(req.conn, code),
                          "can": {"view": True, "edit": held.edit, "remove": held.remove},
                          "members": settings.members(req.conn, code) if held.edit else None})


def update_app(req: Request) -> Response:
    """PATCH /api/miniapps/apps/<code> {title?, icon?}: null clears a field."""
    code = req.params["code"]
    held = _app(req, code, "edit")
    if isinstance(held, Response):
        return held
    body = api._body(req)
    changes = {k: body[k] for k in ("title", "icon") if k in body}
    title = changes.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > 255):
        return error(400, "title must be a string of at most 255 characters")
    if "title" in changes:
        changes["title"] = (title or "").strip() or None  # empty: back to the store's title
    if "icon" in changes:
        icon = None if changes["icon"] == "" else changes["icon"]  # "" clears, as null does
        if icon is not None and (not isinstance(icon, str) or icon not in settings.ICONS):
            return error(400, "icon must be one of the listed icons")
        changes["icon"] = icon

    def go() -> Response:
        settings.update_settings(req.conn, _user(req), code, changes)
        return Response(204)

    return _run(go)


def member_options(req: Request) -> Response:
    """GET /api/miniapps/apps/<code>/member-options: the users and roles an ``edit`` holder may add."""
    held = _app(req, req.params["code"], "edit")
    return held if isinstance(held, Response) else Response(200, settings.member_options(req.conn))


def put_member(req: Request) -> Response:
    """PUT /api/miniapps/apps/<code>/members {username? | role?, can_edit, can_remove}."""
    code = req.params["code"]
    held = _app(req, code, "edit")
    if isinstance(held, Response):
        return held
    body = api._body(req)
    username, role = body.get("username"), body.get("role")
    flags = body.get("can_edit"), body.get("can_remove")
    if not all(isinstance(f, bool) for f in flags):
        return error(400, "can_edit and can_remove must be booleans")
    if (username is None) == (role is None) or not isinstance(username or role, str):
        return error(400, "set exactly one of username or role")
    user_id = None
    if username is not None:
        found = accounts.get_user_by_username(req.conn, username)
        user_id = found.id if found else None
        missing = found is None
    else:
        with req.conn.cursor() as cur:
            cur.execute("SELECT code FROM role WHERE code = %s", (role,))
            missing = cur.fetchone() is None
    if missing:
        return error(404, "no such user or role")

    def go() -> Response:
        settings.put_member(req.conn, _user(req), code, user_id=user_id, role=role, can_edit=flags[0],
                            can_remove=flags[1], may_set_remove=held.remove)
        return Response(204)

    return _run(go)


def delete_member(req: Request) -> Response:
    """DELETE /api/miniapps/apps/<code>/members/<id>: ends the app's live launches."""
    code = req.params["code"]
    held = _app(req, code, "edit")
    if isinstance(held, Response):
        return held
    member_id = int(req.params["id"])
    return _run(lambda: Response(204) if settings.delete_member(req.conn, _user(req), code, member_id)
                else _NOT_FOUND)


def delete_app(req: Request) -> Response:
    """DELETE /api/miniapps/apps/<code>: the one artifact delete (``remove``)."""
    code = req.params["code"]
    held = _app(req, code, "remove")
    if isinstance(held, Response):
        return held

    def go() -> Response:
        va_admin.delete_artifact(req.conn, _user(req), code)
        return Response(204)

    return _run(go)
