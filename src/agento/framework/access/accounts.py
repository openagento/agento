"""Platform users, roles and role grants (PRD E2 §5, §5.1).

Every write owns its transaction: it takes all its ``user`` row locks in one statement in
ascending id order, re-checks the acting admin inside that transaction, writes, revokes what
the change invalidates, and commits. ``create_launch`` locks its user row the same way, so a
launch sees either the old access (and is revoked with the rest) or the new one.
``actor_id=None`` is an operator at the CLI or TUI: no actor check.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import pymysql

from .passwords import dummy_verify, hash_password, verify_password

BUILTIN_ROLES = ("admin", "user")
ROLE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,15}$")
GRANT_KINDS = ("tool", "operation")
ADMIN_OPERATIONS = frozenset({"users.manage", "grants.manage", "config.write", "admin.read", "credentials.manage"})
# Framework operations: checked by grant for every role, admin included (web/api.py create_launch).
FRAMEWORK_OPERATIONS = {"artifact.launch": "Launch a miniapp"}
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_GRANT_LOCK = "agento.role_grant"
MAX_GRANT_NAMES = 1000
_ER_ROW_IS_REFERENCED, _ER_NO_REFERENCED_ROW = 1451, 1452

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


def _check_role(cur, role: str) -> None:
    """Inside the writer's transaction; the FK is the backstop for a role deleted after this."""
    if isinstance(role, str) and ROLE_CODE_RE.fullmatch(role):
        cur.execute("SELECT 1 FROM role WHERE code = %s", (role,))
        if cur.fetchone():
            return
    raise AccessError("unknown role")


def _hash(password: str) -> str:
    try:
        return hash_password(password)
    except ValueError as exc:
        raise AccessError(str(exc)) from None


def create_user(conn, username: str, role: str, password: str | None, *, actor_id: int | None = None) -> User:
    _check_username(username)
    password_hash = _hash(password) if password is not None else None

    def work(cur):
        _lock_actor_and(cur, actor_id)
        _check_role(cur, role)
        try:
            cur.execute(
                "INSERT INTO `user` (username, password_hash, role) VALUES (%s, %s, %s)",
                (username, password_hash, role),
            )
        except pymysql.err.IntegrityError as exc:
            if exc.args[0] == _ER_NO_REFERENCED_ROW:
                raise AccessError("unknown role") from None
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
    if active is not None and not isinstance(active, bool):
        raise AccessError("is_active must be a boolean")
    password_hash = _hash(password) if password is not None else None

    def work(cur):
        _require_target(_lock_actor_and(cur, actor_id, user_id), user_id)
        if role is not None:
            _check_role(cur, role)
            try:
                cur.execute("UPDATE `user` SET role = %s WHERE id = %s", (role, user_id))
            except pymysql.err.IntegrityError:
                raise AccessError("unknown role") from None
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


def declared_tools(*, enabled_only: bool = False) -> set[str]:
    """Every tool name a module declares in ``module.json`` ``tools[]``; with ``enabled_only``,
    of the enabled modules only (what the panel's role tree shows)."""
    from ..bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
    from ..module_discovery import scan_all_modules
    from ..module_status import filter_enabled

    modules = scan_all_modules(CORE_MODULES_DIR, USER_MODULES_DIR)
    if enabled_only:
        modules = filter_enabled(modules)
    return {t["name"] for m in modules for t in m.tools if t.get("name")}


def grantable_operations() -> dict[str, str]:
    """``{id: title}`` of the operations an admin may grant: the built-in ones plus every
    ``acl_resources`` entry a module declares in ``di.json`` (Magento ``acl.xml``). Admin has
    them all built in (``may``/``can_see_*`` check the role first)."""
    from ..bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
    from ..module_discovery import module_dirs_by_name
    from ..module_validator import acl_resource_declarations

    out = dict(FRAMEWORK_OPERATIONS)
    for _name, module_dir in module_dirs_by_name(CORE_MODULES_DIR, USER_MODULES_DIR):
        try:
            manifest = json.loads((module_dir / "module.json").read_text())
        except (OSError, ValueError):
            continue
        out.update(acl_resource_declarations(module_dir, manifest if isinstance(manifest, dict) else {}))
    return out


def is_builtin_resource(role: str, operation: str) -> bool:
    """Admin has every module-declared ACL resource built in (the module checks the role before
    the grant); a framework operation is a grant for every role."""
    return role == "admin" and operation not in FRAMEWORK_OPERATIONS


def _check_scope(workspace_id, agent_view_id) -> None:
    if (workspace_id is None) == (agent_view_id is None):
        raise AccessError("set exactly one of workspace or agent_view")


def _check_grant(kind: str, name: str, workspace_id, agent_view_id) -> None:
    if kind not in GRANT_KINDS:
        raise AccessError(f"grant kind must be one of {', '.join(GRANT_KINDS)}")
    _check_scope(workspace_id, agent_view_id)
    if kind == "operation" and name not in (operations := grantable_operations()):
        raise AccessError(f"operation must be one of {', '.join(sorted(operations))}")
    if kind == "tool" and name not in declared_tools():
        raise AccessError(f"no module declares tool {name!r}")


def add_grant(conn, role: str, kind: str, name: str, *, workspace_id: int | None = None,
              agent_view_id: int | None = None, actor_id: int | None = None) -> int:
    _check_grant(kind, name, workspace_id, agent_view_id)

    def work(cur):
        _lock_actor_and(cur, actor_id)
        _grant_lock(cur)
        _check_role(cur, role)
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
            raise AccessError("unknown role, workspace or agent_view") from None
        return cur.lastrowid

    try:
        return _in_transaction(conn, work)
    finally:
        _release_grant_lock(conn)


def _grant_lock(cur) -> None:
    # role_grant has no unique key (both scope columns are nullable), so a named lock
    # stops two writers from both seeing no duplicate and both inserting.
    cur.execute("SELECT GET_LOCK(%s, 5) AS got", (_GRANT_LOCK,))
    if cur.fetchone()["got"] != 1:
        raise AccessError("busy, retry")


def _release_grant_lock(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DO RELEASE_LOCK(%s)", (_GRANT_LOCK,))


def _revoke_role_launches(cur, role: str, workspace_id, agent_view_id) -> None:
    # A launch is re-authorized per request against its row, not against role_grant.
    scope, value = ("l.agent_view_id = %s", agent_view_id) if agent_view_id is not None \
        else ("l.workspace_id = %s", workspace_id)
    cur.execute(
        "UPDATE launch l JOIN `user` u ON u.id = l.user_id SET l.revoked_at = NOW()"
        f" WHERE u.role = %s AND l.revoked_at IS NULL AND {scope}",
        (role, value),
    )


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
        _revoke_role_launches(cur, grant["role"], grant["workspace_id"], grant["agent_view_id"])

    _in_transaction(conn, work)


def _names(value, what: str) -> set[str]:
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        raise AccessError(f"{what} must be a list of names")
    return set(value)


def set_role_grants(conn, role: str, *, workspace_id: int | None = None, agent_view_id: int | None = None,
                    tools: list[str], operations: list[str], actor_id: int | None = None) -> dict:
    """Make the role's grants at exactly this scope the given names, in one transaction.

    A name already granted at the view's workspace is never inserted at the view (it is
    redundant); a view row that is already there stays. Removing anything ends the role's
    launches in the scope, as ``remove_grant`` does. Returns ``{added, removed}`` (counts).
    """
    _check_scope(workspace_id, agent_view_id)
    want_tools, want_ops = _names(tools, "tools"), _names(operations, "operations")
    if len(want_tools) + len(want_ops) > MAX_GRANT_NAMES:
        raise AccessError(f"at most {MAX_GRANT_NAMES} names per scope")
    if unknown := want_ops - (operations_known := set(grantable_operations())):
        raise AccessError(f"operation must be one of {', '.join(sorted(operations_known))}")
    if unknown := sorted(want_tools - declared_tools()):
        raise AccessError(f"no module declares tool {unknown[0]!r}")
    # ponytail: the tree lists the tools of enabled modules only, so a row for a tool of a
    # disabled module is outside this write and kept (it works again when the module does).
    replaceable = {("tool", n) for n in declared_tools(enabled_only=True)} | {("operation", n) for n in operations_known}
    want = {("tool", n) for n in want_tools} | {("operation", n) for n in want_ops}

    def work(cur):
        locked = _lock_users(cur, "role = %s OR id = %s", (role, actor_id))
        _check_actor(locked, actor_id)
        _grant_lock(cur)
        _check_role(cur, role)
        inherited: set = set()
        if agent_view_id is not None:
            cur.execute("SELECT workspace_id FROM agent_view WHERE id = %s", (agent_view_id,))
            view = cur.fetchone()
            if not view:
                raise AccessError("agent_view not found")
            cur.execute(
                "SELECT grant_kind, name FROM role_grant WHERE role = %s AND workspace_id = %s"
                " AND agent_view_id IS NULL", (role, view["workspace_id"]),
            )
            inherited = {(r["grant_kind"], r["name"]) for r in cur.fetchall()}
        cur.execute(
            "SELECT id, grant_kind, name FROM role_grant WHERE role = %s"
            " AND workspace_id <=> %s AND agent_view_id <=> %s",
            (role, workspace_id, agent_view_id),
        )
        current = cur.fetchall()
        have = {(r["grant_kind"], r["name"]) for r in current}
        # A redundant view row (the workspace grants it too) stays: dropping it changes no access
        # but would end the role's launches here.
        drop = [r["id"] for r in current if (key := (r["grant_kind"], r["name"])) in replaceable
                and key not in want and key not in inherited]
        add = sorted(want - have - inherited)
        if drop:
            cur.execute("DELETE FROM role_grant WHERE id IN (" + ",".join(["%s"] * len(drop)) + ")", drop)
            _revoke_role_launches(cur, role, workspace_id, agent_view_id)
        if add:
            try:
                cur.execute(
                    "INSERT INTO role_grant (role, grant_kind, name, workspace_id, agent_view_id) VALUES "
                    + ",".join(["(%s, %s, %s, %s, %s)"] * len(add)),
                    [v for kind, name in add for v in (role, kind, name, workspace_id, agent_view_id)],
                )
            except pymysql.err.IntegrityError:
                raise AccessError("unknown role, workspace or agent_view") from None
        return {"added": len(add), "removed": len(drop)}

    try:
        return _in_transaction(conn, work)
    finally:
        _release_grant_lock(conn)


def list_roles(conn) -> list[dict]:
    """``[{code, label, builtin, users, scopes}]``: ``scopes`` counts the scopes with a grant."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.code, r.label, COALESCE(u.n, 0) AS users, COALESCE(g.n, 0) AS scopes FROM role r"
            " LEFT JOIN (SELECT role, COUNT(*) AS n FROM `user` GROUP BY role) u ON u.role = r.code"
            # A valid row has exactly one scope, so the two distinct counts never overlap.
            " LEFT JOIN (SELECT role, COUNT(DISTINCT workspace_id) + COUNT(DISTINCT agent_view_id) AS n"
            "  FROM role_grant WHERE (workspace_id IS NULL) <> (agent_view_id IS NULL) GROUP BY role) g"
            " ON g.role = r.code ORDER BY r.label"
        )
        return [{"code": r["code"], "label": r["label"], "builtin": r["code"] in BUILTIN_ROLES,
                 "users": int(r["users"]), "scopes": int(r["scopes"])} for r in cur.fetchall()]


def role_scopes(conn, role: str) -> list[dict]:
    """``[{workspace_id, agent_view_id, tools, operations}]``: grant counts per scope."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT workspace_id, agent_view_id, SUM(grant_kind = 'tool') AS tools,"
            " SUM(grant_kind = 'operation') AS operations FROM role_grant"
            " WHERE role = %s AND (workspace_id IS NULL) <> (agent_view_id IS NULL)"
            " GROUP BY workspace_id, agent_view_id ORDER BY workspace_id, agent_view_id",
            (role,),
        )
        return [{"workspace_id": r["workspace_id"], "agent_view_id": r["agent_view_id"],
                 "tools": int(r["tools"]), "operations": int(r["operations"])} for r in cur.fetchall()]


def _check_label(label) -> str:
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 64:
        raise AccessError("label must have 1 to 64 characters")
    return label.strip()


def _duplicate_role(exc: pymysql.err.IntegrityError) -> AccessError:
    return AccessError("a role with this label already exists" if "uk_role_label" in str(exc)
                       else "a role with this code already exists")


def create_role(conn, code: str, label: str, *, actor_id: int | None = None) -> dict:
    if not isinstance(code, str) or not ROLE_CODE_RE.fullmatch(code):
        raise AccessError("code must match ^[a-z][a-z0-9_]{1,15}$")
    label = _check_label(label)

    def work(cur):
        _lock_actor_and(cur, actor_id)
        try:
            cur.execute("INSERT INTO role (code, label) VALUES (%s, %s)", (code, label))
        except pymysql.err.IntegrityError as exc:
            raise _duplicate_role(exc) from None
        return {"code": code, "label": label, "builtin": code in BUILTIN_ROLES, "users": 0, "scopes": 0}

    return _in_transaction(conn, work)


def _lock_role(cur, code: str) -> None:
    # The grammar first: the column collation is case-insensitive, so 'Admin' would match admin.
    if not isinstance(code, str) or not ROLE_CODE_RE.fullmatch(code):
        raise AccessError("role not found")
    cur.execute("SELECT code FROM role WHERE code = %s FOR UPDATE", (code,))
    if not cur.fetchone():
        raise AccessError("role not found")


def rename_role(conn, code: str, label: str, *, actor_id: int | None = None) -> None:
    """The label only: the code is the key ``user.role`` and ``role_grant.role`` hold."""
    label = _check_label(label)

    def work(cur):
        _lock_actor_and(cur, actor_id)
        _lock_role(cur, code)
        try:
            cur.execute("UPDATE role SET label = %s WHERE code = %s", (label, code))
        except pymysql.err.IntegrityError as exc:
            raise _duplicate_role(exc) from None

    _in_transaction(conn, work)


def _in_use(n: int) -> AccessError:
    return AccessError("1 user has this role" if n == 1 else f"{n} users have this role")


def delete_role(conn, code: str, *, actor_id: int | None = None) -> None:
    """A built-in role or a role with users is refused. Its grants go with it (FK cascade);
    no launch needs a revoke, because no user holds the role."""
    if code in BUILTIN_ROLES:
        raise AccessError("a built-in role cannot be deleted")

    def work(cur):
        _lock_actor_and(cur, actor_id)
        _lock_role(cur, code)
        cur.execute("SELECT COUNT(*) AS n FROM `user` WHERE role = %s", (code,))
        if n := cur.fetchone()["n"]:
            raise _in_use(n)
        try:
            cur.execute("DELETE FROM role WHERE code = %s", (code,))
        except pymysql.err.IntegrityError as exc:
            # A user got the role after the count: the FK (no action) refuses the delete.
            if exc.args[0] == _ER_ROW_IS_REFERENCED:
                raise _in_use(1) from None
            raise

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


def scope_is_active(conn, agent_view_id: int | None) -> bool:
    """Is the scope still live — the view and the workspace holding it?

    A second predicate beside `can_reach()`, never folded into it: E2's admin screens call
    `can_reach()`/`visible_agent_views()` precisely in order to administer a deactivated
    view, so an active check inside them would make such a view unreactivatable. A caller
    that must not act on a dead scope calls the pair.

    `agent_view_id is None` is True: there is no scope left to deactivate. That is the
    deleted-view case (`ON DELETE SET NULL`), not the deactivated-view case.
    """
    if agent_view_id is None:
        return True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM agent_view v JOIN workspace w ON w.id = v.workspace_id "
            "WHERE v.id = %s AND v.is_active = 1 AND w.is_active = 1",
            (agent_view_id,),
        )
        return cur.fetchone() is not None


def may(user: User, operation: str) -> bool:
    return user.role == "admin" and user.is_active and operation in ADMIN_OPERATIONS
