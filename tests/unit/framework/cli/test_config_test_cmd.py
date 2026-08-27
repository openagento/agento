"""config:test — resolve the tester declared on a field path (or --all) and run it."""
from __future__ import annotations

import argparse

import pytest

from agento.framework.cli.config_test_cmd import ConfigTestCommand
from agento.framework.config_test import ERROR, FAIL, NOT_CONFIGURED, OK, TestResult

MODULE = "agento.framework.cli.config_test_cmd"


class _FakeConn:
    """`execute()` closes the connection in a `finally`, so the stub needs
    `close()`. A bare `object()` here raises AttributeError and every test in
    this file would assert on the wrong exception."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_db(monkeypatch):
    """The command must not need a live DB in these tests."""
    monkeypatch.setattr(f"{MODULE}._open_connection", _FakeConn)
    monkeypatch.setattr(f"{MODULE}._resolve_scope", lambda conn, args: ("default", 0))


def _args(**kw):
    base = dict(path=None, all=False, scope="default", scope_id=0, agent_view=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _stub_run(monkeypatch, result, recorder=None):
    """`run_config_test` takes the CONFIG PATH, not a tester name — the whole
    point of declaring on the field. A recorder asserting on a name would be
    asserting on a concept this design deleted."""
    def _run(conn, config_path, *, scope, scope_id, project_root=None):
        if recorder is not None:
            recorder.append((config_path, scope, scope_id))
        return result if not isinstance(result, list) else result.pop(0)
    monkeypatch.setattr(f"{MODULE}.run_config_test", _run)


def _has_tester(monkeypatch, present=True, label="smtp"):
    from pathlib import Path

    from agento.framework.config_test import KIND_TOOLBOX, TesterRef

    ref = None
    if present:
        ref = TesterRef(
            kind=KIND_TOOLBOX, label=label, module="m", module_dir=Path("/m"),
        )
    monkeypatch.setattr(f"{MODULE}.tester_for_field", lambda p: ref)


def test_runs_the_tester_declared_on_the_field(monkeypatch, capsys):
    calls = []
    _has_tester(monkeypatch)
    _stub_run(monkeypatch, TestResult(OK, "connected to mail:587"), calls)
    with pytest.raises(SystemExit) as exc:
        ConfigTestCommand().execute(_args(path="m/smtp_host"))
    assert exc.value.code == 0
    assert calls == [("m/smtp_host", "default", 0)]
    out = capsys.readouterr().out
    assert "OK" in out
    assert "connected to mail:587" in out


def test_a_field_with_no_tester_exits_two(monkeypatch, capsys):
    """Checked before opening a DB connection: a typo'd path is a usage error,
    not a failed test, and must not read as "your credential is broken"."""
    _has_tester(monkeypatch, present=False)
    with pytest.raises(SystemExit) as exc:
        ConfigTestCommand().execute(_args(path="m/other"))
    assert exc.value.code == 2
    err = capsys.readouterr().err.lower()
    assert "no tester" in err


def test_the_usage_error_names_the_manifest_file(monkeypatch, capsys):
    """"declares no tester" is unactionable without saying where a tester would
    be declared. The message names system.json."""
    _has_tester(monkeypatch, present=False)
    with pytest.raises(SystemExit):
        ConfigTestCommand().execute(_args(path="m/other"))
    assert "system.json" in capsys.readouterr().err


def test_a_failing_test_exits_one(monkeypatch, capsys):
    _has_tester(monkeypatch)
    _stub_run(monkeypatch, TestResult(FAIL, "535 5.7.8 authentication failed",
                                      code="AUTH_FAILED"))
    with pytest.raises(SystemExit) as exc:
        ConfigTestCommand().execute(_args(path="m/smtp_host"))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "AUTH_FAILED" in out


def test_an_error_exits_one_but_prints_error_not_fail(monkeypatch, capsys):
    """The exit code merges them; the output must not. "Could not check" printed
    as FAIL sends someone to rotate a credential that was never tested."""
    _has_tester(monkeypatch)
    _stub_run(monkeypatch, TestResult(ERROR, "toolbox unreachable", code="TOOLBOX_UNREACHABLE"))
    with pytest.raises(SystemExit) as exc:
        ConfigTestCommand().execute(_args(path="m/smtp_host"))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "FAIL" not in out


def test_not_configured_exits_zero(monkeypatch, capsys):
    """An optional integration nobody set up must not fail a deploy check."""
    _has_tester(monkeypatch)
    _stub_run(monkeypatch, TestResult(NOT_CONFIGURED, "m/smtp_host is not set"))
    with pytest.raises(SystemExit) as exc:
        ConfigTestCommand().execute(_args(path="m/smtp_host"))
    assert exc.value.code == 0
    assert "NOT_CONFIGURED" in capsys.readouterr().out


def test_the_agent_view_scope_is_passed_through(monkeypatch):
    calls = []
    monkeypatch.setattr(f"{MODULE}._resolve_scope", lambda conn, args: ("agent_view", 7))
    _has_tester(monkeypatch, label="local")
    _stub_run(monkeypatch, TestResult(OK, "keys match"), calls)
    with pytest.raises(SystemExit):
        ConfigTestCommand().execute(
            _args(path="agent_view/identity/ssh_private_key", agent_view="dev")
        )
    assert calls == [("agent_view/identity/ssh_private_key", "agent_view", 7)]


def test_all_runs_every_testable_field(monkeypatch, capsys):
    monkeypatch.setattr(
        f"{MODULE}.enumerate_test_groups",
        lambda: [("a/smtp_pass", ("a/smtp_pass",)), ("b/token", ("b/token",))],
    )
    calls = []
    _stub_run(monkeypatch, TestResult(OK, "fine"), calls)
    with pytest.raises(SystemExit) as exc:
        ConfigTestCommand().execute(_args(all=True))
    assert exc.value.code == 0
    assert [c[0] for c in calls] == ["a/smtp_pass", "b/token"]
    out = capsys.readouterr().out
    assert "a/smtp_pass" in out and "b/token" in out


def test_all_exits_one_when_any_field_fails(monkeypatch):
    monkeypatch.setattr(
        f"{MODULE}.enumerate_test_groups",
        lambda: [("a/smtp_pass", ("a/smtp_pass",)), ("b/token", ("b/token",))],
    )
    _stub_run(monkeypatch, [TestResult(OK, "fine"), TestResult(FAIL, "broken")])
    with pytest.raises(SystemExit) as exc:
        ConfigTestCommand().execute(_args(all=True))
    assert exc.value.code == 1


def test_all_keeps_going_after_the_first_failure(monkeypatch, capsys):
    """A smoke check that stops at the first bad credential hides the other
    three. Every field is reported, then the exit code is decided."""
    monkeypatch.setattr(
        f"{MODULE}.enumerate_test_groups",
        lambda: [("a/smtp_pass", ("a/smtp_pass",)), ("b/token", ("b/token",))],
    )
    _stub_run(monkeypatch, [TestResult(FAIL, "broken"), TestResult(OK, "fine")])
    with pytest.raises(SystemExit) as exc:
        ConfigTestCommand().execute(_args(all=True))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "a/smtp_pass" in out and "b/token" in out


def test_all_probes_a_shared_credential_once_and_names_the_other_fields(
    monkeypatch, capsys
):
    """Six Outlook fields carry one declaration. Probing per field would mean six
    real logins for one credential — rate-limit and lockout pressure for no extra
    information — so `--all` iterates groups and reports the members."""
    monkeypatch.setattr(
        f"{MODULE}.enumerate_test_groups",
        lambda: [("o/client_id", ("o/client_id", "o/client_secret", "o/tenant_id"))],
    )
    calls = []
    _stub_run(monkeypatch, TestResult(OK, "token acquired"), calls)
    with pytest.raises(SystemExit) as exc:
        ConfigTestCommand().execute(_args(all=True))
    assert exc.value.code == 0
    assert [c[0] for c in calls] == ["o/client_id"]      # ONE probe, not three
    out = capsys.readouterr().out
    assert "o/client_secret: same test as o/client_id" in out
    assert "o/tenant_id: same test as o/client_id" in out


def test_all_with_no_testable_fields_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(f"{MODULE}.enumerate_test_groups", lambda: [])
    with pytest.raises(SystemExit) as exc:
        ConfigTestCommand().execute(_args(all=True))
    assert exc.value.code == 0
    assert "no config field" in capsys.readouterr().out.lower()


def test_neither_path_nor_all_exits_two(capsys):
    with pytest.raises(SystemExit) as exc:
        ConfigTestCommand().execute(_args())
    assert exc.value.code == 2


def test_command_metadata():
    cmd = ConfigTestCommand()
    assert cmd.name == "config:test"
    assert cmd.shortcut == "co:te"
    parser = argparse.ArgumentParser()
    cmd.configure(parser)
    parsed = parser.parse_args(["m/smtp_host", "--agent-view", "dev"])
    assert parsed.path == "m/smtp_host"
    assert parsed.agent_view == "dev"
    assert parser.parse_args(["--all"]).all is True
