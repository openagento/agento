from __future__ import annotations

import argparse
import io
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.access import accounts
from agento.framework.cli import access


def _parse(cmd, argv):
    parser = argparse.ArgumentParser()
    cmd.configure(parser)
    return parser.parse_args(argv)


def test_user_create_has_no_password_argument():
    parser = argparse.ArgumentParser()
    access.UserCreateCommand().configure(parser)
    assert not [a for a in parser._actions if "pass" in (a.dest or "")]
    with pytest.raises(SystemExit):
        parser.parse_args(["alice", "--role", "user", "--password", "x"])


@pytest.fixture
def piped_password(monkeypatch):
    """The password arrives on a pipe, never in argv."""
    piped = io.StringIO("correct horse battery\n")
    monkeypatch.setattr(piped, "isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", piped)


@pytest.mark.usefixtures("piped_password")
@patch.object(access, "_connect")
def test_user_create_reads_the_password_from_stdin(mock_connect, monkeypatch):
    create = MagicMock(return_value=accounts.User(1, "alice", "user", True))
    monkeypatch.setattr(accounts, "create_user", create)
    cmd = access.UserCreateCommand()
    cmd.execute(_parse(cmd, ["alice", "--role", "user"]))
    assert create.call_args.args[1:] == ("alice", "user", "correct horse battery")


@pytest.mark.usefixtures("piped_password")
@patch.object(access, "_connect")
def test_user_create_without_role_uses_select(mock_connect, monkeypatch):
    monkeypatch.setattr(accounts, "list_roles", lambda conn: [{"code": "admin"}, {"code": "support"}, {"code": "user"}])
    monkeypatch.setattr("agento.framework.cli.terminal.select", lambda prompt, options: options.index("support"))
    create = MagicMock(return_value=accounts.User(1, "root", "support", True))
    monkeypatch.setattr(accounts, "create_user", create)
    cmd = access.UserCreateCommand()
    cmd.execute(_parse(cmd, ["root"]))
    assert create.call_args.args[1:] == ("root", "support", "correct horse battery")


def test_role_is_any_code_the_db_knows():
    """No hardcoded choices: accounts checks the code against the role table."""
    assert _parse(access.UserCreateCommand(), ["bob", "--role", "support"]).role == "support"
    assert _parse(access.UserSetRoleCommand(), ["bob", "support"]).role == "support"
    assert _parse(access.GrantListCommand(), ["--role", "support"]).role == "support"


@patch.object(access, "_connect")
def test_role_create(mock_connect, monkeypatch):
    create = MagicMock(return_value={"code": "support", "label": "Support desk"})
    monkeypatch.setattr(accounts, "create_role", create)
    cmd = access.RoleCreateCommand()
    cmd.execute(_parse(cmd, ["support", "--label", "Support desk"]))
    assert create.call_args.args[1:] == ("support", "Support desk")
    with pytest.raises(SystemExit):
        _parse(cmd, ["support"])  # --label is required


@patch.object(access, "_connect")
def test_role_list(mock_connect, monkeypatch, capsys):
    monkeypatch.setattr(accounts, "list_roles", lambda conn: [
        {"code": "support", "label": "Support desk", "builtin": False, "users": 2, "scopes": 1}])
    cmd = access.RoleListCommand()
    cmd.execute(_parse(cmd, []))
    assert "users=2" in capsys.readouterr().out


@patch.object(access, "_connect")
def test_role_delete(mock_connect, monkeypatch):
    delete = MagicMock()
    monkeypatch.setattr(accounts, "delete_role", delete)
    cmd = access.RoleDeleteCommand()
    cmd.execute(_parse(cmd, ["support"]))
    assert delete.call_args.args[1:] == ("support",)


@patch.object(access, "_connect")
def test_role_delete_refusal_exits_nonzero(mock_connect, monkeypatch, capsys):
    monkeypatch.setattr(accounts, "delete_role", MagicMock(side_effect=accounts.AccessError("3 users have this role")))
    cmd = access.RoleDeleteCommand()
    with pytest.raises(SystemExit) as exc:
        cmd.execute(_parse(cmd, ["support"]))
    assert exc.value.code == 1 and "3 users" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["--role", "user", "--tool", "t", "--operation", "artifact.launch", "--agent-view", "v"],
    ["--role", "user", "--agent-view", "v"],
    ["--role", "user", "--tool", "t", "--workspace", "w", "--agent-view", "v"],
    ["--role", "user", "--operation", "users.manage", "--agent-view", "v"],
])
def test_grant_add_usage_errors(argv):
    with pytest.raises(SystemExit):
        _parse(access.GrantAddCommand(), argv)


@patch.object(access, "_connect")
def test_grant_add_operation_at_an_agent_view(mock_connect, monkeypatch):
    monkeypatch.setattr("agento.framework.workspace.get_agent_view_by_code",
                        lambda conn, code: MagicMock(id=42) if code == "dev" else None)
    add = MagicMock(return_value=7)
    monkeypatch.setattr(accounts, "add_grant", add)
    cmd = access.GrantAddCommand()
    cmd.execute(_parse(cmd, ["--role", "user", "--operation", "artifact.launch", "--agent-view", "dev"]))
    assert add.call_args.args[1:] == ("user", "operation", "artifact.launch")
    assert add.call_args.kwargs == {"workspace_id": None, "agent_view_id": 42}


@patch.object(access, "_connect")
def test_grant_add_admin_panel_operation_has_no_scope(mock_connect, monkeypatch, capsys):
    add = MagicMock(return_value=8)
    monkeypatch.setattr(accounts, "add_grant", add)
    cmd = access.GrantAddCommand()
    cmd.execute(_parse(cmd, ["--role", "support", "--operation", "admin.users"]))
    assert add.call_args.args[1:] == ("support", "operation", "admin.users")
    assert add.call_args.kwargs == {"workspace_id": None, "agent_view_id": None}
    monkeypatch.setattr(accounts, "list_grants", lambda conn, role: [
        {"id": 8, "role": "support", "grant_kind": "operation", "name": "admin.users",
         "workspace_id": None, "agent_view_id": None}])
    cmd = access.GrantListCommand()
    cmd.execute(_parse(cmd, []))
    assert capsys.readouterr().out.rstrip().endswith("global")


@pytest.mark.parametrize("kind,name,workspace_id,agent_view_id", [
    ("operation", "artifact.launch", None, None),  # a scoped operation needs its scope
    ("tool", "jira_add_comment", None, None),
    ("operation", "admin.users", 1, None),  # an admin panel operation takes none
    ("tool", "admin.users", None, None),
])
def test_check_grant_refuses_the_wrong_scope(kind, name, workspace_id, agent_view_id):
    with pytest.raises(accounts.AccessError):
        accounts._check_grant(kind, name, workspace_id, agent_view_id)


@patch.object(access, "_connect")
def test_service_refusal_exits_nonzero(mock_connect, monkeypatch, capsys):
    monkeypatch.setattr(accounts, "add_grant", MagicMock(side_effect=accounts.AccessError("no module declares tool 'x'")))
    monkeypatch.setattr("agento.framework.workspace.get_agent_view_by_code", lambda conn, code: MagicMock(id=1))
    cmd = access.GrantAddCommand()
    with pytest.raises(SystemExit) as exc:
        cmd.execute(_parse(cmd, ["--role", "user", "--tool", "x", "--agent-view", "dev"]))
    assert exc.value.code == 1
    assert "declares" in capsys.readouterr().err


@patch.object(access, "_connect")
def test_set_role_on_unknown_user_exits(mock_connect, monkeypatch):
    monkeypatch.setattr(accounts, "get_user_by_username", lambda conn, name: None)
    cmd = access.UserSetRoleCommand()
    with pytest.raises(SystemExit):
        cmd.execute(_parse(cmd, ["ghost", "admin"]))
