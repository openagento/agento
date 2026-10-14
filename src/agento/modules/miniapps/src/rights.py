"""Per-app rights (D-MINIAPP-3): the one rule, here and in ``toolbox/launch-source.js``.

``view`` = admin, creator or member (by user or by role); ``edit`` / ``remove`` = admin, creator
or a member with the flag. ``execute`` in a view = ``view`` and an approval there, and admin, or
``artifact.launch`` in the view plus a tool grant for every action of the approved manifest.
The creator is the panel user whose job ran ``init``: an ``account``-trust requester
``user:<id>`` of ``versioned_artifact.created_job_id``. Only the conversation service writes
that trust; a channel requester never matches.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from agento.framework.access import accounts

# One row per artifact the user is creator or member of. JS twin: the view predicate of launch_sql
# (tests/fixtures/miniapp_sql_v2.json). No test compares the text: test_execute_rule_agrees checks
# that both give the same answer over its cases, so change both sides together.
_RIGHTS_SQL = (
    "SELECT va.artifact_code, MAX(j.id IS NOT NULL) AS creator, MAX(m.id IS NOT NULL) AS member,"
    " COALESCE(MAX(m.can_edit), 0) AS can_edit, COALESCE(MAX(m.can_remove), 0) AS can_remove"
    " FROM versioned_artifact va"
    " LEFT JOIN job j ON j.id = va.created_job_id AND j.requester_trust = 'account' AND j.requester_key = %s"
    " LEFT JOIN miniapp_member m ON m.artifact_code = va.artifact_code AND (m.user_id = %s OR m.role = %s)"
    " WHERE {where} GROUP BY va.artifact_code HAVING creator OR member"
)


@dataclass(frozen=True)
class Rights:
    view: bool = False
    edit: bool = False
    remove: bool = False


ALL = Rights(True, True, True)
NONE = Rights()


def is_admin(user: accounts.User) -> bool:
    return user.role == "admin"


def _rows(conn, user: accounts.User, code: str | None) -> dict[str, Rights]:
    where, params = ("va.artifact_code = %s", (code,)) if code is not None else ("1 = 1", ())
    with conn.cursor() as cur:
        cur.execute(_RIGHTS_SQL.format(where=where), (f"user:{user.id}", user.id, user.role, *params))
        rows = cur.fetchall()
    return {r["artifact_code"]: ALL if r["creator"] else Rights(True, bool(r["can_edit"]), bool(r["can_remove"]))
            for r in rows}


def rights(conn, user: accounts.User, code: str) -> Rights:
    if is_admin(user):
        return ALL
    return _rows(conn, user, code).get(code, NONE)


def all_rights(conn, user: accounts.User) -> dict[str, Rights]:
    """The apps the user is creator or member of (an admin's other apps come from the store)."""
    return _rows(conn, user, None)


def manifest_of(row: dict) -> list[str] | None:
    """``miniapp_activation.manifest_actions``: None for an approval made before it existed."""
    m = row.get("manifest_actions")
    m = json.loads(m) if isinstance(m, (str, bytes)) else m
    return m if isinstance(m, list) else None


def view_grants(conn, user: accounts.User, view_ids) -> dict[int, tuple[bool, set[str]]]:
    """Per view: ``(artifact.launch held, tool grants)`` of the user's role, in one query.

    A view grant or a workspace grant of the view's workspace, as ``accounts.has_operation``."""
    ids = sorted(set(view_ids))
    if is_admin(user) or not ids:
        return {v: (True, set()) for v in ids}
    out: dict[int, tuple[bool, set[str]]] = {v: (False, set()) for v in ids}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT v.id, g.grant_kind, g.name FROM agent_view v JOIN role_grant g ON g.role = %s"
            " AND ((g.agent_view_id = v.id AND g.workspace_id IS NULL)"
            " OR (g.agent_view_id IS NULL AND g.workspace_id = v.workspace_id))"
            " AND (g.grant_kind = 'tool' OR (g.grant_kind = 'operation' AND g.name = 'artifact.launch'))"
            f" WHERE v.id IN ({','.join(['%s'] * len(ids))})", (user.role, *ids))
        for r in cur.fetchall():
            tools = out[r["id"]][1]
            if r["grant_kind"] == "tool":
                tools.add(r["name"])
            else:
                out[r["id"]] = (True, tools)
    return out


def executes(user: accounts.User, manifest: list[str] | None, grants: tuple[bool, set[str]]) -> bool:
    """The view part of ``execute`` (the caller has checked ``view`` and the approval)."""
    if is_admin(user):
        return True  # S4; is_enabled and allowed_actions still bound every call
    launch, tools = grants
    return launch and manifest is not None and set(manifest) <= tools


def can_execute(conn, user: accounts.User, code: str, view_id: int) -> bool:
    if not rights(conn, user, code).view:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT manifest_actions FROM miniapp_activation WHERE artifact_code = %s AND agent_view_id = %s",
                    (code, view_id))
        row = cur.fetchone()
    return row is not None and executes(user, manifest_of(row), view_grants(conn, user, [view_id])[view_id])
