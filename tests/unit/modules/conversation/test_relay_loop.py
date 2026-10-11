"""`conversation:relay` loops inside one cron invocation, as the one relay (WI-7)."""
from __future__ import annotations

import pytest

from agento.modules.conversation.src import commands


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.conn.sql.append(sql)
        self._got = self.conn.got

    def fetchone(self):
        return {"got": self._got}


class _Conn:
    def __init__(self, got=1):
        self.got = got
        self.sql = []

    def cursor(self):
        return _Cursor(self)


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def calls(monkeypatch):
    calls = {"relay": 0, "project": 0, "prune": 0}

    def bump(key, value):
        def f(conn, **kw):
            calls[key] += 1
            return value
        return f

    monkeypatch.setattr(commands.relay, "relay_outbox", bump("relay", 2))
    monkeypatch.setattr(commands.relay, "project_tool_calls", bump("project", 1))
    monkeypatch.setattr(commands.relay, "prune_relayed", bump("prune", 3))
    monkeypatch.setattr(commands.service, "config", lambda conn, path: 7)
    return calls


def test_it_passes_every_second_until_the_budget_ends(calls):
    clock = _Clock()
    totals = commands.run_relay(_Conn(), clock=clock, sleep=clock.sleep)
    passes = calls["relay"]
    assert passes == commands.RELAY_BUDGET_S // commands.RELAY_PASS_S
    assert calls["project"] == passes
    assert set(clock.sleeps) == {commands.RELAY_PASS_S}
    assert clock.now < commands.RELAY_BUDGET_S
    assert totals == (2 * passes, passes, 3)


def test_it_prunes_once_per_invocation(calls):
    clock = _Clock()
    commands.run_relay(_Conn(), clock=clock, sleep=clock.sleep)
    assert calls["prune"] == 1


def test_a_held_lock_makes_it_exit_with_no_relay(calls):
    clock = _Clock()
    conn = _Conn(got=0)
    assert commands.run_relay(conn, clock=clock, sleep=clock.sleep) is None
    assert calls == {"relay": 0, "project": 0, "prune": 0}
    assert clock.sleeps == []
    assert not any("RELEASE_LOCK" in s for s in conn.sql)


def test_it_takes_the_lock_without_waiting_and_releases_it(calls):
    clock = _Clock()
    conn = _Conn()
    commands.run_relay(conn, clock=clock, sleep=clock.sleep)
    assert "GET_LOCK" in conn.sql[0]
    assert "RELEASE_LOCK" in conn.sql[-1]


def test_it_releases_the_lock_when_a_pass_fails(calls, monkeypatch):
    def boom(conn, **kw):
        raise RuntimeError("db gone")

    monkeypatch.setattr(commands.relay, "relay_outbox", boom)
    clock = _Clock()
    conn = _Conn()
    with pytest.raises(RuntimeError):
        commands.run_relay(conn, clock=clock, sleep=clock.sleep)
    assert "RELEASE_LOCK" in conn.sql[-1]
