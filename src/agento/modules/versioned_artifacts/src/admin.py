"""Artifact administration for the panel: list, history, make current, delete (D-MINIAPP-2).

``web`` reads and writes the DB directly; every read or write of the store goes through the
toolbox ``/admin`` endpoint, as the panel user (``toolbox_client.web_subject``).
"""
from __future__ import annotations

from agento.framework.access import accounts, launches
from agento.framework.event_manager import get_event_manager
from agento.framework.events import ArtifactDeleteAfterEvent
from agento.web import api, toolbox_client

_HISTORY_SQL = (
    "SELECT version_id, operation, job_id, agent_view_id, actor, description, created_at"
    " FROM versioned_artifact_audit WHERE artifact_code = %s AND result = 'ok' AND version_id IN ({})"
    " ORDER BY id"
)


class AdminError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.extra, self.error_code = status, extra, None


def _store(conn, user: accounts.User, method: str, path: str, body: dict | None = None) -> dict:
    r = toolbox_client.admin_request(conn, method, f"versioned-artifacts{path}",
                                     subject_id=toolbox_client.web_subject(user), body=body)
    if r.status == 200:
        return r.body
    if r.status in (400, 404, 409):
        extra = {"current_version": r.body["current_version"]} if "current_version" in r.body else {}
        err = AdminError(r.status, str(r.body.get("message") or "refused"), **extra)
        err.error_code = r.body.get("error_code")
        raise err
    raise AdminError(503, "toolbox unavailable")


def list_artifacts(conn, user: accounts.User) -> list[dict]:
    """Every artifact of the store (global: the leaf ``versioned_artifacts`` is not scoped)."""
    listed = _store(conn, user, "GET", "").get("artifacts")
    if not isinstance(listed, list):
        raise AdminError(503, "toolbox unavailable")
    return listed


def history(conn, user: accounts.User, code: str) -> list[dict]:
    """The versions (newest first, from the store), each with its audit rows (one query)."""
    versions = _store(conn, user, "GET", f"/{code}/versions").get("versions")
    if not isinstance(versions, list):
        raise AdminError(503, "toolbox unavailable")
    ids = [v["version_id"] for v in versions]
    events: dict[str, list[dict]] = {v: [] for v in ids}
    if ids:
        with conn.cursor() as cur:
            cur.execute(_HISTORY_SQL.format(",".join(["%s"] * len(ids))), (code, *ids))
            for row in cur.fetchall():
                events[row.pop("version_id")].append({**row, "created_at": api._iso(row["created_at"])})
    return [{"version_id": v["version_id"], "preview_path": v.get("preview_path"), "events": events[v["version_id"]]}
            for v in versions]


def make_current(conn, user: accounts.User, code: str, version_id: str, expected_current_version: str) -> dict:
    """Point ``current`` at a version (a rollback too): a compare-and-set in the toolbox."""
    return _store(conn, user, "POST", f"/{code}/publish",
                  {"version_id": version_id, "expected_current_version": expected_current_version})


def delete_artifact(conn, user: accounts.User, code: str) -> None:
    """Delete an artifact: the one delete path (plan §2.4). Idempotent, so a retry finishes it.

    Under the retention lock that approve and launch hold: revoke every live launch first
    (a failure raises and nothing is removed), then the store, then the row, then the event.
    """
    with launches.retention_lock(conn, code):
        launches.revoke_for_artifact(conn, code)
        try:
            _store(conn, user, "DELETE", f"/{code}")
        except AdminError as exc:
            if exc.error_code != "ARTIFACT_NOT_FOUND":  # already gone: finish the cleanup
                raise
        with conn.cursor() as cur:
            cur.execute("DELETE FROM versioned_artifact WHERE artifact_code = %s", (code,))
        conn.commit()
        get_event_manager().dispatch("artifact_delete_after", ArtifactDeleteAfterEvent(artifact_code=code))
