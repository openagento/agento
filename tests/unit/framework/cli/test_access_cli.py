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


@patch.object(access, "_connect")
def test_user_create_reads_the_password_from_stdin(mock_connect, monkeypatch):
    piped = io.StringIO("correct horse battery\n")
    monkeypatch.setattr(piped, "isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", piped)
    create = MagicMock(return_value=accounts.User(1, "alice", "user", True))
    monkeypatch.setattr(accounts, "create_user", create)
    cmd = access.UserCreateCommand()
    cmd.execute(_parse(cmd, ["alice", "--role", "user"]))
    assert create.call_args.args[1:] == ("alice", "user", "correct horse battery")


@patch.object(access, "_connect")
def test_user_create_without_role_uses_select(mock_connect, monkeypatch):
    piped = io.StringIO("correct horse battery")
    monkeypatch.setattr(piped, "isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", piped)
    monkeypatch.setattr("agento.framework.cli.terminal.select", lambda prompt, options: options.index("admin"))
    create = MagicMock(return_value=accounts.User(1, "root", "admin", True))
    monkeypatch.setattr(accounts, "create_user", create)
    cmd = access.UserCreateCommand()
    cmd.execute(_parse(cmd, ["root"]))
    assert create.call_args.args[2] == "admin"


@pytest.mark.parametrize("argv", [
    ["--role", "user", "--tool", "t", "--operation", "artifact.launch", "--agent-view", "v"],
    ["--role", "user", "--agent-view", "v"],
    ["--role", "user", "--tool", "t", "--workspace", "w", "--agent-view", "v"],
    ["--role", "user", "--tool", "t"],
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
