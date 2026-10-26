"""The writes of an app's settings and members (D-MINIAPP-3): one function per transition.

Each writes its row and a ``versioned_artifact_audit`` row in one transaction. No event: the
data is miniapps-private and the audit row is the trace. A write that can drop someone's
``execute`` (a member delete) ends the app's live launches after the commit.
"""
from __future__ import annotations

import json
from pathlib import Path

import pymysql

from agento.framework.access import accounts, launches
from agento.web.toolbox_client import web_subject as actor

from .activation import _AUDIT_SQL, ActivationError

ICONS = frozenset(json.loads((Path(__file__).resolve().parents[1] / "panel" / "icons.json").read_text()))
_ER_NO_REFERENCED_ROW = 1452
_SETTINGS = ("title", "icon")


def _write(conn, work):
    try:
        return accounts._in_transaction(conn, work)
    except pymysql.err.IntegrityError as exc:
        if exc.args[0] == _ER_NO_REFERENCED_ROW:  # no versioned_artifact row (a best-effort INSERT failed)
            raise ActivationError(409, "app metadata missing") from exc
        raise


def settings(conn, code: str) -> dict:
    """The app's title and icon override (None: not set) and the store's title."""
    with conn.cursor() as cur:
        cur.execute("SELECT va.title AS default_title, m.title, m.icon FROM versioned_artifact va"
                    " LEFT JOIN miniapp m ON m.artifact_code = va.artifact_code WHERE va.artifact_code = %s", (code,))
        return cur.fetchone() or {"default_title": None, "title": None, "icon": None}


def update_settings(conn, user: accounts.User, code: str, changes: dict) -> None:
    """``changes``: a subset of ``title`` / ``icon``, already checked; None clears the field."""
    cols = [c for c in _SETTINGS if c in changes]
    if not cols:
        return

    def work(cur):
        cur.execute(f"INSERT INTO miniapp (artifact_code, {', '.join(cols)}) VALUES (%s{', %s' * len(cols)})"
                    f" ON DUPLICATE KEY UPDATE {', '.join(f'{c} = VALUES({c})' for c in cols)}",
                    (code, *(changes[c] for c in cols)))
        cur.execute(_AUDIT_SQL, (code, "miniapp.settings", None, None, actor(user), ",".join(cols)))

    _write(conn, work)


def members(conn, code: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT m.id, m.user_id, u.username, m.role, m.can_edit, m.can_remove FROM miniapp_member m"
                    " LEFT JOIN `user` u ON u.id = m.user_id WHERE m.artifact_code = %s ORDER BY m.id", (code,))
        return [{**r, "can_edit": bool(r["can_edit"]), "can_remove": bool(r["can_remove"])} for r in cur.fetchall()]


def member_options(conn) -> dict:
    """What a member can be: the active users' names and the roles (the add-member list)."""
    with conn.cursor() as cur:
        cur.execute("SELECT username FROM `user` WHERE is_active = 1 ORDER BY username")
        users = [r["username"] for r in cur.fetchall()]
        cur.execute("SELECT code, label FROM role ORDER BY label")
        return {"users": users, "roles": list(cur.fetchall())}


def put_member(conn, user: accounts.User, code: str, *, user_id: int | None, role: str | None,
               can_edit: bool, can_remove: bool, may_set_remove: bool) -> None:
    """Add or change one member: a user or a role, never both (the table cannot CHECK it).

    ``can_remove`` changes only for a caller who holds ``remove`` (no escalation). A flag
    change keeps ``view``, so it drops nobody's ``execute``: no launch is revoked."""
    if (user_id is None) == (role is None):
        raise ActivationError(400, "set exactly one of username or role")
    key, value = ("user_id", user_id) if user_id is not None else ("role", role)

    def work(cur):
        cur.execute(f"SELECT can_remove FROM miniapp_member WHERE artifact_code = %s AND {key} = %s FOR UPDATE",
                    (code, value))
        row = cur.fetchone()
        if bool(row and row["can_remove"]) != can_remove and not may_set_remove:
            raise ActivationError(403, "only a user who may remove the app may change remove")
        cur.execute(f"INSERT INTO miniapp_member (artifact_code, {key}, can_edit, can_remove) VALUES (%s, %s, %s, %s)"
                    " ON DUPLICATE KEY UPDATE can_edit = VALUES(can_edit), can_remove = VALUES(can_remove)",
                    (code, value, int(can_edit), int(can_remove)))
        cur.execute(_AUDIT_SQL, (code, "miniapp.member_put", None, None, actor(user),
                                 f"{key} {value} edit={int(can_edit)} remove={int(can_remove)}"))

    _write(conn, work)


def delete_member(conn, user: accounts.User, code: str, member_id: int) -> bool:
    def work(cur):
        cur.execute("SELECT user_id, role FROM miniapp_member WHERE id = %s AND artifact_code = %s FOR UPDATE",
                    (member_id, code))
        row = cur.fetchone()
        if row is None:
            return False
        cur.execute("DELETE FROM miniapp_member WHERE id = %s", (member_id,))
        who = f"user_id {row['user_id']}" if row["user_id"] is not None else f"role {row['role']}"
        cur.execute(_AUDIT_SQL, (code, "miniapp.member_delete", None, None, actor(user), who))
        # ponytail: ends every live launch of the app; per-user revoke when users complain.
        launches.revoke_artifact_launches(cur, code)
        return True

    # The retention lock orders this against a launch being created (its rights re-check
    # runs under the same lock): no launch outlives the membership that allowed it.
    with launches.retention_lock(conn, code):
        return _write(conn, work)
