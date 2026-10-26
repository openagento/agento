"""Backend calls to the toolbox: one panel tool call (a single-use `user_session`
capability on E1's invoke route), one miniapp action, or one store-admin call (a viewless
`internal_rest` capability on the `/admin` endpoint).

The raw capability lives only in this call's locals: it travels in the Authorization
header (never a query string), and nothing here logs or returns it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from agento.framework.access.sessions import Session
from agento.framework.auth_context import resolve_auth_ttls
from agento.framework.config_test.toolbox import resolve_toolbox_url
from agento.framework.toolbox_capability import issue_capability, rest_capability

_UNAVAILABLE = {"ok": False, "error": {"code": "toolbox_unavailable", "message": "toolbox unavailable"}}


@dataclass(frozen=True)
class InvokeResult:
    status: int
    body: dict


def invoke_tool(conn, session: Session, tool: str, arguments: dict, *, workspace_id: int,
                agent_view_id: int | None, timeout: float = 30.0) -> InvokeResult:
    with conn.cursor() as cur:
        cur.execute("SELECT TIMESTAMPDIFF(SECOND, NOW(), expires_at) AS left_s FROM session WHERE id = %s",
                    (session.id,))
        row = cur.fetchone()
    # The capability never outlives the session it is minted from (the verifier checks it too).
    ttl = min(resolve_auth_ttls(conn, workspace_id)["capability_ttl"], row["left_s"] if row else 0)
    if ttl <= 0:
        return InvokeResult(401, {"ok": False, "error": {"code": "unauthorized", "message": "session expired"}})
    token = issue_capability(
        conn, kind="user_session", agent_view_id=agent_view_id, workspace_id=workspace_id,
        ttl_seconds=ttl, allowed_transports=["http"], subject_id=str(session.user.id), source_id=session.id,
    )
    return _post(conn, tool, arguments, token, timeout)


_LIVE_LAUNCH_SQL = (
    "SELECT l.id, l.artifact_code, l.version_id, l.workspace_id, l.agent_view_id, l.allowed_actions,"
    " TIMESTAMPDIFF(SECOND, NOW(), l.expires_at) AS left_s FROM launch l"
    " WHERE l.id = %s AND l.user_id = %s AND l.exchange_redeemed_at IS NOT NULL"
    " AND l.revoked_at IS NULL AND l.expires_at > NOW()"
)
_NOT_FOUND = {"ok": False, "error": {"code": "not_found", "message": "not found"}}


def invoke_launch_action(conn, session: Session, launch_id: str, tool: str, arguments: dict, *,
                         timeout: float = 30.0) -> InvokeResult:
    """One miniapp action: a single-use `miniapp` capability bound to the launch (PRD E6 §10).

    Its ceiling is the launch's allowed actions, so no broad capability ever serves a
    miniapp; the toolbox re-checks the launch, the activation and the grants per call.
    """
    with conn.cursor() as cur:
        cur.execute(_LIVE_LAUNCH_SQL, (launch_id, session.user.id))
        launch = cur.fetchone()
    if not launch:
        return InvokeResult(404, _NOT_FOUND)
    actions = launch["allowed_actions"]
    actions = json.loads(actions) if isinstance(actions, (str, bytes)) else actions
    if not isinstance(actions, list) or tool not in actions:
        return InvokeResult(403, {"ok": False, "error": {"code": "forbidden", "message": "action not allowed"}})
    ttl = min(resolve_auth_ttls(conn, launch["workspace_id"])["capability_ttl"], launch["left_s"] or 0)
    if ttl <= 0:
        return InvokeResult(404, _NOT_FOUND)
    token = issue_capability(
        conn, kind="miniapp", agent_view_id=launch["agent_view_id"], workspace_id=launch["workspace_id"],
        ttl_seconds=ttl, allowed_transports=["http"], subject_id=str(session.user.id), source_id=launch["id"],
        app={"artifact_code": launch["artifact_code"], "version_id": launch["version_id"], "launch_id": launch["id"]},
        tool_ceiling=list(actions),
    )
    return _post(conn, tool, arguments, token, timeout)


def web_subject(user) -> str:
    """The subject ``web`` mints for a panel user's store call: the toolbox audit actor (D-MINIAPP-2)."""
    return f"service:web:user:{user.id}"


def admin_request(conn, method: str, path: str, *, subject_id: str, body: dict | None = None,
                  timeout: float = 30.0) -> InvokeResult:
    """One call to ``/admin/<path>`` on the toolbox (store administration).

    The capability is viewless ``internal_rest``, the only kind the ``admin`` endpoint
    admits; ``subject_id`` (``service:…``) names who acts, and the toolbox audits it. It is
    revoked when the one request returns.
    """
    with rest_capability(agent_view_id=None, subject_id=subject_id) as token:
        return _send(method, f"{resolve_toolbox_url(conn)}/admin/{path}", body, token, timeout)


def _post(conn, tool: str, arguments: dict, token: str, timeout: float) -> InvokeResult:
    return _send("POST", f"{resolve_toolbox_url(conn)}/internal/tools/{tool}:invoke", arguments, token, timeout)


def _send(method: str, url: str, body: dict | None, token: str, timeout: float) -> InvokeResult:
    try:
        r = httpx.request(method, url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    except httpx.HTTPError:
        return InvokeResult(503, _UNAVAILABLE)
    try:
        body = r.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return InvokeResult(502, _UNAVAILABLE)
    return InvokeResult(r.status_code, body)
