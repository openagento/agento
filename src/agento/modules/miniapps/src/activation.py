"""Miniapp approval: one row per (artifact, agent_view) that pins a version (D-MINIAPP-2).

The pin is the version's content (``content_id``), not its id: a version id can repeat after
an artifact is deleted and made again. Web owns ``miniapp_activation``; every read of the git
store goes through the toolbox ``/admin`` endpoint. Approve and launch hold the artifact's
retention lock from the store read to the commit, so prune cannot remove what they pin.
"""
from __future__ import annotations

import json
import logging
import re

from agento.framework.access import accounts, launches
from agento.web import api, app_path, toolbox_client
from agento.web.toolbox_client import web_subject as actor

log = logging.getLogger(__name__)

_CONTENT_ID = re.compile(r"[0-9a-f]{40}")

APPROVE_SQL = (
    "INSERT INTO miniapp_activation (artifact_code, agent_view_id, approved_version_id, approved_content_id,"
    " allowed_actions, activated_by) VALUES (%s, %s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE"
    " approved_version_id = VALUES(approved_version_id), approved_content_id = VALUES(approved_content_id),"
    " allowed_actions = VALUES(allowed_actions), activated_by = VALUES(activated_by), activated_at = NOW()"
)
_AUDIT_SQL = (
    "INSERT INTO versioned_artifact_audit (artifact_code, operation, version_id, agent_view_id, actor, result,"
    " description) VALUES (%s, %s, %s, %s, %s, 'ok', %s)"
)


class ActivationError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.extra = status, extra


def _resolve(conn, user: accounts.User, code: str, version: str) -> dict | None:
    """The store's answer for ``version`` (or ``current``): its served tree restored, its
    content id and its manifest. None when the artifact or the version is not there."""
    r = toolbox_client.admin_request(conn, "POST", f"miniapps/{code}/resolve", subject_id=actor(user),
                                     body={"version": version})
    if r.status == 404:
        return None
    if r.status == 400 and version == "current":
        raise ActivationError(409, "artifact has no published version")
    b = r.body
    if (r.status != 200 or not isinstance(b.get("version_id"), str)
            or not app_path.VERSION_ID_RE.fullmatch(b["version_id"])
            or not isinstance(b.get("content_id"), str) or not _CONTENT_ID.fullmatch(b["content_id"])):
        raise ActivationError(503, "toolbox unavailable")
    return b


def approve(conn, user: accounts.User, code: str, view_id: int, expected_version_id: str,
            actions: list[str] | None) -> dict:
    """Approve ``current`` for the view; only what the agent published can be approved."""
    with launches.retention_lock(conn, code):
        r = _resolve(conn, user, code, "current")
        if r is None:
            raise ActivationError(404, "not found")
        if r["version_id"] != expected_version_id:
            raise ActivationError(409, "current version changed, reload", current_version=r["version_id"])
        manifest = r.get("manifest")
        if not isinstance(manifest, dict) or not isinstance(manifest.get("actions"), list):
            raise ActivationError(409, "manifest invalid")
        allowed = manifest["actions"] if actions is None else list(dict.fromkeys(actions))
        if any(a not in manifest["actions"] for a in allowed):
            raise ActivationError(400, "an action is not in the manifest")

        def work(cur):
            cur.execute(APPROVE_SQL, (code, view_id, r["version_id"], r["content_id"], json.dumps(allowed),
                                      actor(user)))
            cur.execute(_AUDIT_SQL, (code, "miniapp.approve", r["version_id"], view_id, actor(user),
                                     f"actions: {','.join(allowed)}"[:255]))

        accounts._in_transaction(conn, work)
    return {"artifact_code": code, "agent_view_id": view_id, "approved_version_id": r["version_id"],
            "allowed_actions": allowed}


def approval(conn, user: accounts.User, code: str, view_id: int) -> dict:
    """The approval page: the view's approval (or None) and ``current`` with its manifest.

    The resolve may serve a pruned tree again: idempotent, and approve does the same step."""
    r = _resolve(conn, user, code, "current")
    if r is None:
        raise ActivationError(404, "not found")
    with conn.cursor() as cur:
        cur.execute("SELECT approved_version_id, allowed_actions, activated_by, activated_at FROM miniapp_activation"
                    " WHERE artifact_code = %s AND agent_view_id = %s", (code, view_id))
        row = cur.fetchone()
    if row is not None:
        row["allowed_actions"] = json.loads(row["allowed_actions"])
        row["activated_at"] = api._iso(row["activated_at"])
    m = r.get("manifest")
    m = m if isinstance(m, dict) else {}
    return {"approval": row,
            "current": {"version_id": r["version_id"], "title": m.get("title"), "actions": m.get("actions")}}


def approvals(conn, code: str, view_ids: list[int]) -> list[dict]:
    """The artifact's approvals in ``view_ids`` (the views the caller may launch in)."""
    if not view_ids:
        return []
    with conn.cursor() as cur:
        cur.execute("SELECT agent_view_id, approved_version_id, allowed_actions, activated_by, activated_at"
                    " FROM miniapp_activation WHERE artifact_code = %s AND agent_view_id IN"
                    f" ({','.join(['%s'] * len(view_ids))}) ORDER BY agent_view_id", (code, *view_ids))
        rows = cur.fetchall()
    return [{**r, "allowed_actions": json.loads(r["allowed_actions"]), "activated_at": api._iso(r["activated_at"])}
            for r in rows]


def revoke(conn, user: accounts.User, code: str, view_id: int) -> bool:
    """Delete the approval. Live launches lose their actions at the next call (the checker reads the row)."""
    def work(cur):
        cur.execute("SELECT approved_version_id FROM miniapp_activation WHERE artifact_code = %s"
                    " AND agent_view_id = %s FOR UPDATE", (code, view_id))
        row = cur.fetchone()
        if row is None:
            return False
        cur.execute("DELETE FROM miniapp_activation WHERE artifact_code = %s AND agent_view_id = %s",
                    (code, view_id))
        cur.execute(_AUDIT_SQL, (code, "miniapp.revoke", row["approved_version_id"], view_id, actor(user), None))
        return True

    return accounts._in_transaction(conn, work)


def launch(conn, user: accounts.User, code: str, workspace_id: int, view_id: int,
           preview: bool) -> tuple[launches.Launch, str]:
    """A preview launches ``current`` with no actions; else the approved version with its actions."""
    with launches.retention_lock(conn, code):
        if preview:
            r = _resolve(conn, user, code, "current")
            if r is None:
                raise ActivationError(404, "not found")
            version, actions = r["version_id"], []
        else:
            with conn.cursor() as cur:
                cur.execute("SELECT approved_version_id, approved_content_id, allowed_actions FROM miniapp_activation"
                            " WHERE artifact_code = %s AND agent_view_id = %s", (code, view_id))
                row = cur.fetchone()
            if row is None:
                raise ActivationError(404, "not found")
            version = row["approved_version_id"]
            r = _resolve(conn, user, code, version)
            if r is None or r["content_id"] != row["approved_content_id"]:
                # The approved bytes are gone: the approval goes with them (hygiene; the pin
                # check above is what refuses).
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM miniapp_activation WHERE artifact_code = %s AND agent_view_id = %s"
                                " AND approved_content_id = %s", (code, view_id, row["approved_content_id"]))
                conn.commit()
                raise ActivationError(409, "approved version gone")
            actions = row["allowed_actions"]
            actions = json.loads(actions) if isinstance(actions, (str, bytes)) else actions
        return launches.create_launch(conn, user, workspace_id=workspace_id, agent_view_id=view_id,
                                      artifact_code=code, version_id=version, allowed_actions=actions)


def catalogue(conn, user: accounts.User, view_id: int) -> list[dict]:
    """The view's approved apps, with the store's title and ``current``. No manifest read per row."""
    with conn.cursor() as cur:
        cur.execute("SELECT artifact_code, approved_version_id FROM miniapp_activation WHERE agent_view_id = %s"
                    " ORDER BY artifact_code", (view_id,))
        rows = cur.fetchall()
    if not rows:
        return []
    r = toolbox_client.admin_request(conn, "GET", "versioned-artifacts", subject_id=actor(user))
    listed = r.body.get("artifacts") if r.status == 200 else None
    if not isinstance(listed, list) or not all(isinstance(a, dict) for a in listed):
        raise ActivationError(503, "toolbox unavailable")
    store = {a.get("artifact_code"): a for a in listed}
    out = []
    for row in rows:
        code, approved = row["artifact_code"], row["approved_version_id"]
        a = store.get(code)
        if a is None:
            log.warning("miniapp %s is approved in view %s but not in the store", code, view_id)
            continue
        out.append({"artifact_code": code, "version_id": approved, "title": a.get("title") or code,
                    "current_version": a.get("current_version"),
                    "update_waiting": a.get("current_version") != approved})
    return out
