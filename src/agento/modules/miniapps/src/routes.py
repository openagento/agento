"""The miniapps REST surface under ``/api/miniapps/`` (D-MINIAPP-2).

Each route names its leaf in ``di.json`` ``acl`` and ``web`` checks it before the handler (403,
D-ADMIN-ACL-2). The ``artifact.launch`` check is scoped to the view, so the handler makes it,
and it answers 404: telling a user that a scope or an app exists is disclosure.
"""
from __future__ import annotations

from agento.framework.access import accounts, launches
from agento.web import api, app_path
from agento.web.api import Request, Response, error

from . import activation

_NOT_FOUND = error(404, "not found")


def _user(req: Request) -> accounts.User:
    assert req.session is not None  # every module route is auth='session'
    return req.session.user


def _launch_right(req: Request, view_id: int) -> tuple[int, int] | Response:
    """The view's ``(workspace_id, view_id)`` when the user may launch there, else 404."""
    scope = api._resolve_scope(req, {"agent_view_id": view_id})
    if isinstance(scope, Response):
        return scope
    if not accounts.has_operation(req.conn, _user(req).role, "artifact.launch", *scope):
        return _NOT_FOUND
    return scope


def _run(fn) -> Response:
    try:
        return fn()
    except activation.ActivationError as exc:
        return Response(exc.status, {"error": str(exc), **exc.extra})
    except launches.RetentionBusy:
        return error(503, "artifact busy, try again")


def _approval_target(req: Request) -> tuple[str, int] | Response:
    code, view_id = req.params["code"], int(req.params["view"])
    if not app_path.ARTIFACT_CODE_RE.fullmatch(code):
        return _NOT_FOUND
    scope = _launch_right(req, view_id)
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


def approvals(req: Request) -> Response:
    """GET /api/miniapps/apps/<code>/approvals: the artifact's approvals in the views the user may launch in."""
    code = req.params["code"]
    if not app_path.ARTIFACT_CODE_RE.fullmatch(code):
        return _NOT_FOUND
    return Response(200, activation.approvals(req.conn, code, [v["id"] for v in _launch_views(req)]))


def _launch_views(req: Request) -> list[dict]:
    return accounts.operation_views(req.conn, _user(req).role, "artifact.launch")


def launch_views(req: Request) -> Response:
    """GET /api/miniapps/agent-views: the views where the user may launch. The panel probe too
    (open to a user and to an approver; the catalogue still needs ``miniapps``)."""
    return Response(200, [{k: v[k] for k in ("id", "code", "label", "workspace_id")} for v in _launch_views(req)])


def _launch(req: Request, *, preview: bool) -> Response:
    body = api._body(req)
    view_id, code = body.get("agent_view_id"), body.get("artifact_code")
    if not api._positive_int(view_id) or body.get("workspace_id") is not None:
        return error(400, "agent_view_id is required")
    if not isinstance(code, str) or not app_path.ARTIFACT_CODE_RE.fullmatch(code):
        return error(400, "artifact_code must match ^[a-z0-9][a-z0-9-]{0,63}$")
    if "preview" in body:
        return error(400, "preview is a route of its own: POST /api/miniapps/previews")
    scope = _launch_right(req, view_id)
    if isinstance(scope, Response):
        return scope
    user = _user(req)

    def go() -> Response:
        try:
            launch, exchange_code = activation.launch(req.conn, user, code, *scope, preview)
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
    """POST /api/miniapps/launches {agent_view_id, artifact_code}: the approved version."""
    return _launch(req, preview=False)


def create_preview(req: Request) -> Response:
    """POST /api/miniapps/previews {agent_view_id, artifact_code}: ``current`` with no actions,
    approved or not. Its route needs ``miniapps.approve``."""
    return _launch(req, preview=True)


def catalogue(req: Request) -> Response:
    """GET /api/miniapps/agent-views/<id>/catalogue: the apps approved for the view."""
    view_id = int(req.params["id"])
    scope = api._resolve_scope(req, {"agent_view_id": view_id})
    if isinstance(scope, Response):
        return scope
    # The view is reachable, so it is already disclosed: no launch right means nothing to list.
    if not accounts.has_operation(req.conn, _user(req).role, "artifact.launch", *scope):
        return Response(200, [])
    return _run(lambda: Response(200, activation.catalogue(req.conn, _user(req), view_id)))
