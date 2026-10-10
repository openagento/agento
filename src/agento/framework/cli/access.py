"""CLI commands ``user:*``, ``role:*`` and ``grant:*`` — the operator side of PRD E2 §5.

Same service functions as the web admin API (``framework/access/accounts.py``). A password
never comes from argv: it is read from stdin (piped) or ``getpass`` (TTY).
"""
from __future__ import annotations

import argparse
import getpass
import sys
from typing import NoReturn

from ..access import accounts
from ..access.accounts import AccessError
from ..db import get_connection_or_exit
from .runtime import _load_framework_config


def _fail(message: str) -> NoReturn:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _read_password() -> str:
    if sys.stdin.isatty():
        first = getpass.getpass("Password: ")
        if getpass.getpass("Repeat password: ") != first:
            _fail("passwords do not match")
        return first
    return sys.stdin.read().rstrip("\r\n")


def _connect():
    db_config, _, _ = _load_framework_config()
    return get_connection_or_exit(db_config)


def _user_or_fail(conn, username: str) -> accounts.User:
    user = accounts.get_user_by_username(conn, username)
    if user is None:
        _fail(f"user '{username}' not found")
    return user


class _Command:
    shortcut = ""

    def execute(self, args: argparse.Namespace) -> None:
        conn = _connect()
        try:
            self.run(conn, args)
        except AccessError as exc:
            _fail(str(exc))
        finally:
            conn.close()


class UserCreateCommand(_Command):
    name = "user:create"
    shortcut = "us:cr"
    help = "Create a panel user (password from stdin or a prompt, never argv)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("username")
        parser.add_argument("--role", default=None, help="Role code (role:list); omit to choose interactively")

    def run(self, conn, args) -> None:
        if args.role is None:
            from .terminal import select

            codes = [r["code"] for r in accounts.list_roles(conn)]
            args.role = codes[select("Role:", codes)]
        user = accounts.create_user(conn, args.username, args.role, _read_password())
        print(f"Created: {user.username} ({user.role})")


class UserListCommand(_Command):
    name = "user:list"
    shortcut = "us:li"
    help = "List panel users"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def run(self, conn, args) -> None:
        for u in accounts.list_users(conn):
            print(f"  {u.username:<32} {u.role:<16} {'active' if u.is_active else 'inactive'}")


class UserSetRoleCommand(_Command):
    name = "user:set-role"
    shortcut = "us:sr"
    help = "Change a user's role (ends that user's sessions and launches)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("username")
        parser.add_argument("role", help="Role code (role:list)")

    def run(self, conn, args) -> None:
        accounts.set_role(conn, _user_or_fail(conn, args.username).id, args.role)
        print(f"Role of {args.username}: {args.role}")


class UserActivateCommand(_Command):
    name = "user:activate"
    shortcut = "us:ac"
    help = "Allow a user to log in again"
    active = True

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("username")

    def run(self, conn, args) -> None:
        accounts.set_active(conn, _user_or_fail(conn, args.username).id, self.active)
        print(f"{args.username}: {'active' if self.active else 'inactive'}")


class UserDeactivateCommand(UserActivateCommand):
    name = "user:deactivate"
    shortcut = "us:de"
    help = "Stop a user from logging in (ends that user's sessions and launches)"
    active = False


class UserPasswordCommand(_Command):
    name = "user:password"
    shortcut = "us:pa"
    help = "Set a user's password (stdin or a prompt; ends that user's sessions)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("username")

    def execute(self, args: argparse.Namespace) -> None:
        args.password = _read_password()
        super().execute(args)

    def run(self, conn, args) -> None:
        accounts.set_password(conn, _user_or_fail(conn, args.username).id, args.password)
        print(f"Password set: {args.username}")


def _scope_ids(conn, args) -> tuple[int | None, int | None]:
    if args.workspace is not None:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM workspace WHERE code = %s", (args.workspace,))
            row = cur.fetchone()
        if not row:
            _fail(f"workspace '{args.workspace}' not found")
        return row["id"], None
    from ..workspace import get_agent_view_by_code

    view = get_agent_view_by_code(conn, args.agent_view)
    if view is None:
        _fail(f"agent_view '{args.agent_view}' not found")
    return None, view.id


class GrantAddCommand(_Command):
    name = "grant:add"
    shortcut = "gr:ad"
    help = "Grant a role a tool or an operation in one workspace or agent_view"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--role", required=True, help="Role code (role:list)")
        what = parser.add_mutually_exclusive_group(required=True)
        what.add_argument("--tool")
        operations = accounts.grantable_operations()
        what.add_argument("--operation", choices=sorted(operations),
                          help="; ".join(f"{k}: {v}" for k, v in sorted(operations.items())))
        where = parser.add_mutually_exclusive_group(required=True)
        where.add_argument("--workspace", help="Workspace code")
        where.add_argument("--agent-view", dest="agent_view", help="Agent view code")

    def run(self, conn, args) -> None:
        workspace_id, agent_view_id = _scope_ids(conn, args)
        kind, name = ("tool", args.tool) if args.tool else ("operation", args.operation)
        grant_id = accounts.add_grant(conn, args.role, kind, name, workspace_id=workspace_id,
                                      agent_view_id=agent_view_id)
        print(f"Grant {grant_id}: {args.role} may use {kind} {name}")


class GrantRemoveCommand(_Command):
    name = "grant:remove"
    shortcut = "gr:re"
    help = "Remove a grant (ends that role's launches in its scope)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("grant_id", type=int)

    def run(self, conn, args) -> None:
        accounts.remove_grant(conn, args.grant_id)
        print(f"Removed grant {args.grant_id}")


class GrantListCommand(_Command):
    name = "grant:list"
    shortcut = "gr:li"
    help = "List role grants"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--role", default=None, help="Role code (role:list)")

    def run(self, conn, args) -> None:
        for g in accounts.list_grants(conn, args.role):
            scope = f"agent_view={g['agent_view_id']}" if g["agent_view_id"] else f"workspace={g['workspace_id']}"
            print(f"  {g['id']:>5}  {g['role']:<16} {g['grant_kind']:<9} {g['name']:<40} {scope}")


class RoleListCommand(_Command):
    name = "role:list"
    shortcut = "ro:li"
    help = "List roles with their users and the scopes they have access in"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def run(self, conn, args) -> None:
        for r in accounts.list_roles(conn):
            builtin = "built-in" if r["builtin"] else ""
            print(f"  {r['code']:<16} {r['label']:<32} users={r['users']:<4} scopes={r['scopes']:<4} {builtin}")


class RoleCreateCommand(_Command):
    name = "role:create"
    shortcut = "ro:cr"
    help = "Create a role (no access until it is granted some)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("code", help="^[a-z][a-z0-9_]{1,15}$; cannot be changed later")
        parser.add_argument("--label", required=True, help="Display name, unique")

    def run(self, conn, args) -> None:
        role = accounts.create_role(conn, args.code, args.label)
        print(f"Created role: {role['code']} ({role['label']})")


class RoleDeleteCommand(_Command):
    name = "role:delete"
    shortcut = "ro:de"
    help = "Delete a role and its grants (refused for a built-in role or a role with users)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("code")

    def run(self, conn, args) -> None:
        accounts.delete_role(conn, args.code)
        print(f"Deleted role: {args.code}")


ACCESS_COMMANDS = [
    UserCreateCommand, UserListCommand, UserSetRoleCommand, UserActivateCommand, UserDeactivateCommand,
    UserPasswordCommand, RoleListCommand, RoleCreateCommand, RoleDeleteCommand,
    GrantAddCommand, GrantRemoveCommand, GrantListCommand,
]
