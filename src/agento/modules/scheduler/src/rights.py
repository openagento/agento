"""Per-schedule rights (AG-67 plan §7, D-3, D-12, D-13).

A row is in one group for the caller: **mine** (I own it), **shared** (a ``schedule_member``
row for me), **agent** (an agent_view owns it) or **user** (another user owns it). Each group
has three panel leaves (read, ``.write``, ``.share``); agent and user rows also need the target
view in the caller's visible views. A share gives read and write only (D-3). Admin holds every
leaf. Changing the owner is admin only (D-12). A row of a read-only source (a Jira periodic
row) gives read only, to everyone. A soft-deleted row gives nothing.

The JS twin of the tools' reach is ``toolbox/schedule-tools.js`` (it does not use this matrix:
an agent reaches only its own view's rows, OP-1).
"""
from __future__ import annotations

from typing import NamedTuple

from agento.framework.access import accounts

SOURCE = "scheduler"  # the one editable source: every write path also checks it in SQL
READ_LEAVES = ("scheduler.own", "scheduler.agent", "scheduler.user")
# A row with the names the panel shows. Plain reads: the routes lock nothing until they write.
ROW_SQL = ("SELECT s.*, u.username AS owner_username, v.code AS agent_view_code FROM schedule s"
           " LEFT JOIN `user` u ON u.id = s.owner_user_id LEFT JOIN agent_view v ON v.id = s.agent_view_id")


class Rights(NamedTuple):
    read: bool
    write: bool
    share: bool
    delete: bool
    change_owner: bool


NONE = Rights(False, False, False, False, False)


def group(user: accounts.User, row: dict, *, member: bool) -> str:
    if row["owner_user_id"] == user.id:
        return "mine"
    if member:
        return "shared"
    return "agent" if row["owner_agent_view_id"] is not None else "user"


def decide(user: accounts.User, row: dict, *, held: set[str], visible: set[int], member: bool) -> Rights:
    """The pure matrix: ``held`` are the caller's panel leaves, ``visible`` the ids of the views
    they see, ``member`` whether the row is shared with them."""
    if row["deleted_at"] is not None or not user.is_active:
        return NONE
    admin = user.role == "admin"
    read = write = share = admin
    in_view = row["agent_view_id"] in visible
    for name, applies in (
        ("own", row["owner_user_id"] == user.id),
        ("agent", in_view and row["owner_agent_view_id"] is not None),
        ("user", in_view and row["owner_user_id"] not in (None, user.id)),
    ):
        if applies and f"scheduler.{name}" in held:
            read = True
            write |= f"scheduler.{name}.write" in held
            share |= f"scheduler.{name}.share" in held
    if member:
        read = write = True
    if row["source"] != SOURCE:
        return Rights(read, False, False, False, False)
    return Rights(read, write, share, share, admin)


def held_leaves(conn, user: accounts.User) -> set[str]:
    return set(accounts.held_admin(conn, user.role)) if user.is_active else set()


def visible_ids(conn, user: accounts.User) -> set[int]:
    return {v["id"] for v in accounts.visible_agent_views(conn, user)}


def is_member(conn, schedule_id: int, user_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM schedule_member WHERE schedule_id = %s AND user_id = %s",
                    (schedule_id, user_id))
        return cur.fetchone() is not None


def rights(conn, user: accounts.User, row: dict) -> Rights:
    return decide(user, row, held=held_leaves(conn, user), visible=visible_ids(conn, user),
                  member=is_member(conn, row["id"], user.id))


def may_create(conn, user: accounts.User) -> bool:
    """Create needs ``scheduler.own`` (D-13); the target view is checked by the caller."""
    return "scheduler.own" in held_leaves(conn, user)


def may_list(conn, user: accounts.User) -> bool:
    """The probe: a read leaf, or a share."""
    if not held_leaves(conn, user).isdisjoint(READ_LEAVES):
        return True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM schedule_member WHERE user_id = %s LIMIT 1", (user.id,))
        return user.is_active and cur.fetchone() is not None


def list_groups(conn, user: accounts.User) -> dict[str, list[tuple[dict, Rights]]]:
    """Every row the caller may read, by group, with its rights. Two reads of rows: the
    candidates (owned, shared, or in a visible view; every row for admin) and the share rows."""
    held, visible = held_leaves(conn, user), visible_ids(conn, user)
    with conn.cursor() as cur:
        cur.execute("SELECT schedule_id FROM schedule_member WHERE user_id = %s", (user.id,))
        shared = {r["schedule_id"] for r in cur.fetchall()}
        where, params = "1 = 1", ()
        if user.role != "admin":
            marks = ",".join(["%s"] * len(visible)) or "NULL"
            where = (f"(s.owner_user_id = %s OR s.agent_view_id IN ({marks}) OR s.id IN "
                     "(SELECT m.schedule_id FROM schedule_member m WHERE m.user_id = %s))")
            params = (user.id, *visible, user.id)
        cur.execute(f"{ROW_SQL} WHERE s.deleted_at IS NULL AND {where} ORDER BY s.id", params)
        rows = list(cur.fetchall())
    groups: dict[str, list[tuple[dict, Rights]]] = {"mine": [], "shared": [], "agent": [], "user": []}
    for row in rows:
        member = row["id"] in shared
        r = decide(user, row, held=held, visible=visible, member=member)
        if r.read:
            groups[group(user, row, member=member)].append((row, r))
    return groups
