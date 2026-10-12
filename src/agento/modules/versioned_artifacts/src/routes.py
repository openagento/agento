"""The artifacts admin REST surface under ``/api/versioned_artifacts/`` (D-MINIAPP-2).

A module route cannot name an admin leaf in ``di.json`` yet, so each handler checks its leaf
with ``accounts.may`` (403), the same way the miniapps routes do.
"""
from __future__ import annotations

from agento.framework.access import accounts, launches
from agento.web import api, app_path
from agento.web.api import Request, Response, error

from . import admin

_FORBIDDEN = error(403, "forbidden")


def _run(req: Request, leaf: str, fn) -> Response:
    """``fn(user)`` when the session's user holds ``leaf``; store errors become their status."""
    assert req.session is not None  # every module route is auth='session'
    user = req.session.user
    if not accounts.may(req.conn, user, leaf):
        return _FORBIDDEN
    try:
        return fn(user)
    except admin.AdminError as exc:
        return Response(exc.status, {"error": str(exc), **exc.extra})
    except launches.RetentionBusy:
        return error(503, "artifact busy, try again")


def list_artifacts(req: Request) -> Response:
    """GET /api/versioned_artifacts/artifacts: every artifact of the store."""
    return _run(req, "admin.artifacts", lambda user: Response(200, admin.list_artifacts(req.conn, user)))


def versions(req: Request) -> Response:
    """GET /api/versioned_artifacts/artifacts/<code>/versions: versions with their audit rows."""
    return _run(req, "admin.artifacts",
                lambda user: Response(200, admin.history(req.conn, user, req.params["code"])))


def make_current(req: Request) -> Response:
    """POST /api/versioned_artifacts/artifacts/<code>/publish {version_id, expected_current_version}."""
    def go(user) -> Response:
        body = api._body(req)
        version, expected = body.get("version_id"), body.get("expected_current_version")
        if not all(isinstance(v, str) and app_path.VERSION_ID_RE.fullmatch(v) for v in (version, expected)):
            return error(400, "version_id and expected_current_version are required")
        return Response(200, admin.make_current(req.conn, user, req.params["code"], version, expected))

    return _run(req, "admin.artifacts.publish", go)


def delete(req: Request) -> Response:
    """DELETE /api/versioned_artifacts/artifacts/<code>: store, row, live launches and approvals."""
    def go(user) -> Response:
        admin.delete_artifact(req.conn, user, req.params["code"])
        return Response(204)

    return _run(req, "admin.artifacts.delete", go)
