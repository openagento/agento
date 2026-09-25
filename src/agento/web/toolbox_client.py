"""One panel tool call: mint a single-use `user_session` capability and invoke E1's route.

The raw capability lives only in this call's locals: it travels in the Authorization
header (never a query string), and nothing here logs or returns it.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from agento.framework.access.sessions import Session
from agento.framework.auth_context import resolve_auth_ttls
from agento.framework.config_test.toolbox import resolve_toolbox_url
from agento.framework.toolbox_capability import issue_capability

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
    try:
        r = httpx.post(
            f"{resolve_toolbox_url(conn)}/internal/tools/{tool}:invoke",
            json=arguments, headers={"Authorization": f"Bearer {token}"}, timeout=timeout,
        )
    except httpx.HTTPError:
        return InvokeResult(503, _UNAVAILABLE)
    try:
        body = r.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return InvokeResult(502, _UNAVAILABLE)
    return InvokeResult(r.status_code, body)
