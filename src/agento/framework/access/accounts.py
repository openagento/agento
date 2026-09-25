"""Platform users, roles and role grants (PRD E2 §5, §5.1).

Every write owns its transaction: it takes all its ``user`` row locks in one statement in
ascending id order, re-checks the acting admin inside that transaction, writes, revokes what
the change invalidates, and commits. ``create_launch`` locks its user row the same way, so a
launch sees either the old access (and is revoked with the rest) or the new one.
``actor_id=None`` is an operator at the CLI or TUI: no actor check.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pymysql

from .passwords import dummy_verify, hash_password, verify_password

ROLES = ("admin", "user")
GRANT_KINDS = ("tool", "operation")
ADMIN_OPERATIONS = frozenset({"users.manage", "grants.manage", "config.write"})
GRANTABLE_OPERATIONS = frozenset({"artifact.launch"})
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_GRANT_LOCK = "agento.role_grant"

# A view grant reaches only a call scoped to that view; a workspace grant reaches the
# workspace and every view in it. A row with both or neither scope matches nothing.
_SCOPE_MATCH = (
    "((agent_view_id = %s AND workspace_id IS NULL)"
    " OR (agent_view_id IS NULL AND workspace_id = %s))"
)


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    is_active: bool


class AccessError(ValueError):
    """A refused access change; the message is safe to show an admin."""


def _user(row: dict) -> User:
    return User(id=row["id"], username=row["username"], role=row["role"], is_active=bool(row["is_active"]))


def _lock_users(cur, where: str, params: tuple) -> dict[int, dict]:
    cur.execute(
        f"SELECT id, role, is_active FROM `user` WHERE {where} ORDER BY id FOR UPDATE", params,
    )
    return {r["id"]: r for r in cur.fetchall()}


def _check_actor(locked: dict[int, dict], actor_id: int | None) -> None:
    if actor_id is None:
        return
    row = locked.get(actor_id)
    if not row or row["role"] != "admin" or not row["is_active"]:
        raise AccessError("not allowed")


def _lock_actor_and(cur, actor_id: int | None, *user_ids: int) -> dict[int, dict]:
    ids = sorted({i for i in (actor_id, *user_ids) if i is not None})
    if not ids:
        return {}
    locked = _lock_users(cur, "id IN (" + ",".join(["%s"] * len(ids)) + ")", tuple(ids))
    _check_actor(locked, actor_id)
    return locked


def _in_transaction(conn, work):
    conn.begin()
    try:
        with conn.cursor() as cur:
            result = work(cur)
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise


def _revoke_sessions(cur, user_id: int) -> None:
    cur.execute("UPDATE session SET revoked_at = NOW() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))


def _revoke_launches(cur, user_id: int) -> None:
    cur.execute("UPDATE launch SET revoked_at = NOW() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))


def _check_username(username: str) -> None:
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        raise AccessError("username must match ^[a-z0-9][a-z0-9._-]{0,63}$")


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise AccessError(f"role must be one of {', '.join(ROLES)}")


def _hash(password: str) -> str:
    try:
        return hash_password(password)
    except ValueError as exc:
        raise AccessError(str(exc)) from None


def create_user(conn, username: str, role: str, password: str | None, *, actor_id: int | None = None) -> User:
    _check_username(username)
    _check_role(role)
    password_hash = _hash(password) if password is not None else None

    def work(cur):
        _lock_actor_and(cur, actor_id)
        try:
            cur.execute(
                "INSERT INTO `user` (username, password_hash, role) VALUES (%s, %s, %s)",
                (username, password_hash, role),
            )
        except pymysql.err.IntegrityError:
            raise AccessError("username already exists") from None
        return User(id=cur.lastrowid, username=username, role=role, is_active=True)

    return _in_transaction(conn, work)


def get_user(conn, user_id: int) -> User | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id, username, role, is_active FROM `user` WHERE id = %s", (user_id,))
        row = cur.fetchone()
    return _user(row) if row else None


def get_user_by_username(conn, username: str) -> User | None:
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT id, username, role, is_active FROM `user` WHERE username = %s", (username,))
        row = cur.fetchone()
    return _user(row) if row else None


def authenticate(conn, username: str, password: str) -> User | None:
    with conn.cursor() as cur:
        return authenticate_in(cur, username, password)


def authenticate_in(cur, username: str, password: str, *, lock: bool = False) -> User | None:
    """``lock=True`` holds the user row until the caller's transaction ends (sign-in)."""
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        dummy_verify(password)
        return None
    cur.execute(
        "SELECT id, username, role, is_active, password_hash FROM `user` WHERE username = %s"
        + (" FOR UPDATE" if lock else ""), (username,),
    )
    row = cur.fetchone()
    ok = verify_password(password, row["password_hash"] if row else None)
    if not ok or not row["is_active"]:
        return None
    return _user(row)


def list_users(conn) -> list[User]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, username, role, is_active FROM `user` ORDER BY username")
        return [_user(r) for r in cur.fetchall()]


def _require_target(locked: dict[int, dict], user_id: int) -> dict:
    if user_id not in locked:
        raise AccessError("user not found")
    return locked[user_id]


def update_user(conn, user_id: int, *, role: str | None = None, active: bool | None = None,
                password: str | None = None, actor_id: int | None = None) -> None:
    """Apply every given field in one transaction: all of them or none."""
    if role is not None:
        _check_role(role)
    if active is not None and not isinstance(active, bool):
        raise AccessError("is_active must be a boolean")
    password_hash = _hash(password) if password is not None else None

    def work(cur):
        _require_target(_lock_actor_and(cur, actor_id, user_id), user_id)
        if role is not None:
            cur.execute("UPDATE `user` SET role = %s WHERE id = %s", (role, user_id))
        if active is not None:
            cur.execute("UPDATE `user` SET is_active = %s WHERE id = %s", (1 if active else 0, user_id))
        if password_hash is not None:
            cur.execute("UPDATE `user` SET password_hash = %s WHERE id = %s", (password_hash, user_id))
        if role is not None or active is False or password_hash is not None:
            _revoke_sessions(cur, user_id)
        if role is not None or active is False:
            _revoke_launches(cur, user_id)

    _in_transaction(conn, work)


def set_role(conn, user_id: int, role: str, *, actor_id: int | None = None) -> None:
    update_user(conn, user_id, role=role, actor_id=actor_id)


def set_active(conn, user_id: int, active: bool, *, actor_id: int | None = None) -> None:
    update_user(conn, user_id, active=active, actor_id=actor_id)


def set_password(conn, user_id: int, password: str, *, actor_id: int | None = None) -> None:
    update_user(conn, user_id, password=password, actor_id=actor_id)


def declared_tools() -> set[str]:
    """Every tool name a module declares in ``module.json`` ``tools[]``, enabled or not."""
    from ..bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
    from ..module_discovery import scan_all_modules

    return {t["name"] for m in scan_all_modules(CORE_MODULES_DIR, USER_MODULES_DIR) for t in m.tools if t.get("name")}


def _check_grant(role: str, kind: str, name: str, workspace_id, agent_view_id) -> None:
    _check_role(role)
    if kind not in GRANT_KINDS:
        raise AccessError(f"grant kind must be one of {', '.join(GRANT_KINDS)}")
    if (workspace_id is None) == (agent_view_id is None):
        raise AccessError("set exactly one of workspace or agent_view")
    if kind == "operation" and name not in GRANTABLE_OPERATIONS:
        raise AccessError(f"operation must be one of {', '.join(sorted(GRANTABLE_OPERATIONS))}")
    if kind == "tool" and name not in declared_tools():
        raise AccessError(f"no module declares tool {name!r}")


def add_grant(conn, role: str, kind: str, name: str, *, workspace_id: int | None = None,
              agent_view_id: int | None = None, actor_id: int | None = None) -> int:
    _check_grant(role, kind, name, workspace_id, agent_view_id)

    def work(cur):
        _lock_actor_and(cur, actor_id)
        # role_grant has no unique key (both scope columns are nullable), so a named lock
        # stops two writers from both seeing no duplicate and both inserting.
        cur.execute("SELECT GET_LOCK(%s, 5) AS got", (_GRANT_LOCK,))
        if cur.fetchone()["got"] != 1:
            raise AccessError("busy, retry")
        cur.execute(
            "SELECT id FROM role_grant WHERE role = %s AND grant_kind = %s AND name = %s"
            " AND workspace_id <=> %s AND agent_view_id <=> %s",
            (role, kind, name, workspace_id, agent_view_id),
        )
        row = cur.fetchone()
        if row:
            return row["id"]
        try:
            cur.execute(
                "INSERT INTO role_grant (role, grant_kind, name, workspace_id, agent_view_id)"
                " VALUES (%s, %s, %s, %s, %s)",
                (role, kind, name, workspace_id, agent_view_id),
            )
        except pymysql.err.IntegrityError:
            raise AccessError("unknown workspace or agent_view") from None
        return cur.lastrowid

    try:
        return _in_transaction(conn, work)
    finally:
        with conn.cursor() as cur:
            cur.execute("DO RELEASE_LOCK(%s)", (_GRANT_LOCK,))


def remove_grant(conn, grant_id: int, *, actor_id: int | None = None) -> None:
    def work(cur):
        cur.execute("SELECT role, workspace_id, agent_view_id FROM role_grant WHERE id = %s", (grant_id,))
        grant = cur.fetchone()
        if not grant:
            raise AccessError("grant not found")
        locked = _lock_users(cur, "role = %s OR id = %s", (grant["role"], actor_id))
        _check_actor(locked, actor_id)
        cur.execute("DELETE FROM role_grant WHERE id = %s", (grant_id,))
        if cur.rowcount != 1:
            raise AccessError("grant not found")
        if grant["agent_view_id"] is not None:
            scope, value = "l.agent_view_id = %s", grant["agent_view_id"]
        else:
            scope, value = "l.workspace_id = %s", grant["workspace_id"]
        # A launch is re-authorized per request against its row, not against role_grant.
        cur.execute(
            "UPDATE launch l JOIN `user` u ON u.id = l.user_id SET l.revoked_at = NOW()"
            f" WHERE u.role = %s AND l.revoked_at IS NULL AND {scope}",
            (grant["role"], value),
        )

    _in_transaction(conn, work)


def list_grants(conn, role: str | None = None) -> list[dict]:
    sql = "SELECT id, role, grant_kind, name, workspace_id, agent_view_id, created_at FROM role_grant"
    params: tuple = ()
    if role is not None:
        sql, params = sql + " WHERE role = %s", (role,)
    with conn.cursor() as cur:
        cur.execute(sql + " ORDER BY role, grant_kind, name, id", params)
        return list(cur.fetchall())


def _granted(conn, role: str, kind: str, workspace_id, agent_view_id) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT name FROM role_grant WHERE role = %s AND grant_kind = %s AND {_SCOPE_MATCH}"
            " ORDER BY name",
            (role, kind, agent_view_id, workspace_id),
        )
        return [r["name"] for r in cur.fetchall()]


def permitted_tools(conn, role: str, workspace_id: int | None, agent_view_id: int | None) -> list[str]:
    return _granted(conn, role, "tool", workspace_id, agent_view_id)


def has_operation(conn, role: str, operation: str, workspace_id: int | None, agent_view_id: int | None) -> bool:
    return operation in _granted(conn, role, "operation", workspace_id, agent_view_id)


def visible_agent_views(conn, user: User) -> list[dict]:
    sql = "SELECT v.id, v.code, v.label, v.workspace_id FROM agent_view v"
    params: tuple = ()
    if user.role != "admin":
        sql += (
            " WHERE EXISTS (SELECT 1 FROM role_grant g WHERE g.role = %s AND ("
            "(g.agent_view_id = v.id AND g.workspace_id IS NULL)"
            " OR (g.agent_view_id IS NULL AND g.workspace_id = v.workspace_id)))"
        )
        params = (user.role,)
    with conn.cursor() as cur:
        cur.execute(sql + " ORDER BY v.code", params)
        return list(cur.fetchall())


def can_reach(conn, user: User, *, workspace_id: int | None, agent_view_id: int | None) -> bool:
    if user.role == "admin":
        return True
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM role_grant WHERE role = %s AND {_SCOPE_MATCH} LIMIT 1",
            (user.role, agent_view_id, workspace_id),
        )
        return cur.fetchone() is not None


def may(user: User, operation: str) -> bool:
    return user.role == "admin" and user.is_active and operation in ADMIN_OPERATIONS
