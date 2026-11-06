"""artifacts:prune — config days, --days, --before, --dry-run/--print output, the single-run lock."""
from __future__ import annotations

import argparse
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agento.modules.core.src.commands import artifacts_prune
from agento.modules.core.src.commands.artifacts_prune import ArtifactsPruneCommand


class _Conn:
    """Answers GET_LOCK and the isolation query; records every statement in order."""

    def __init__(self, got_lock: bool = True):
        self.got_lock = got_lock
        self.sql: list[str] = []
        self.closed = False
        self._last = ""

    def cursor(self):
        conn = self
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = lambda *a: False

        def execute(sql, params=None):
            conn.sql.append(sql)
            conn._last = sql

        def fetchone():
            if "GET_LOCK" in conn._last:
                return {"got": 1 if conn.got_lock else 0}
            if "transaction_isolation" in conn._last:
                return {"iso": "READ-COMMITTED"}
            return None

        cur.execute.side_effect = execute
        cur.fetchone.side_effect = fetchone
        cur.fetchall.return_value = []
        return cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _args(dry_run=False, print_files=False, days=None, before=None):
    return SimpleNamespace(dry_run=dry_run, print_files=print_files, days=days, before=before)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr("agento.framework.artifacts_retention.ARTIFACTS_DIR", str(tmp_path))
    return tmp_path


def _run(args, conn, days=30):
    with patch("agento.framework.cli.runtime._load_framework_config",
               return_value=(MagicMock(), None, None)), \
         patch("agento.framework.db.get_connection", return_value=conn), \
         patch.object(artifacts_prune, "_configured_days", return_value=days) as configured:
        ArtifactsPruneCommand().execute(args)
    return configured


def _old_dir(root: Path) -> Path:
    """A run dir with no job row, last touched 40 days ago."""
    run = root / "acme" / "dev" / "7"
    run.mkdir(parents=True)
    (run / "out.txt").write_text("x" * 10)
    old = (datetime.now(UTC) - timedelta(days=40)).timestamp()
    os.utime(run / "out.txt", (old, old))
    os.utime(run, (old, old))
    return run


@pytest.fixture
def no_harness_paths():
    """No registered harness keeps persistent paths: nothing to copy back."""
    with patch("agento.framework.harness.list_harnesses", return_value=[MagicMock()]), \
         patch("agento.framework.harness.persistent_home_paths_for", return_value=[]):
        yield


def test_command_reads_days_from_config(root):
    with patch("agento.framework.artifacts_retention.prune_run_dirs") as runs, \
         patch("agento.framework.artifacts_retention.prune_thread_dirs") as threads:
        _run(_args(), _Conn(), days=12)
    expected = _utcnow() - timedelta(days=12)
    for prune in (runs, threads):
        assert abs(prune.call_args.kwargs["cutoff"] - expected) < timedelta(minutes=1)


def test_days_flag_overrides(root):
    with patch("agento.framework.artifacts_retention.prune_run_dirs") as runs, \
         patch("agento.framework.artifacts_retention.prune_thread_dirs"):
        configured = _run(_args(days=5), _Conn(), days=12)
    expected = _utcnow() - timedelta(days=5)
    assert abs(runs.call_args.kwargs["cutoff"] - expected) < timedelta(minutes=1)
    configured.assert_not_called()


def test_before_is_the_cutoff_at_midnight_utc(root):
    with patch("agento.framework.artifacts_retention.prune_run_dirs") as runs, \
         patch("agento.framework.artifacts_retention.prune_thread_dirs") as threads:
        configured = _run(_args(before=date(2026, 10, 1)), _Conn(), days=12)
    assert runs.call_args.kwargs["cutoff"] == datetime(2026, 10, 1)
    assert threads.call_args.kwargs["cutoff"] == datetime(2026, 10, 1)
    configured.assert_not_called()


def test_before_removes_only_what_ended_before_the_date(root, no_harness_paths, capsys):
    run = _old_dir(root)  # 40 days old
    _run(_args(before=(_utcnow() - timedelta(days=50)).date()), _Conn())
    assert run.exists()
    _run(_args(before=(_utcnow() - timedelta(days=30)).date()), _Conn())
    assert not run.exists()


def test_before_after_today_is_refused(root):
    conn = _Conn()
    with patch("agento.framework.artifacts_retention.prune_run_dirs") as runs, \
         pytest.raises(SystemExit, match="after today"):
        _run(_args(before=_utcnow().date() + timedelta(days=1)), conn)
    runs.assert_not_called()
    assert conn.sql == []


def test_before_and_days_cannot_be_combined():
    parser = argparse.ArgumentParser()
    ArtifactsPruneCommand().configure(parser)
    assert parser.parse_args(["--before", "2026-10-01"]).before == date(2026, 10, 1)
    with pytest.raises(SystemExit):
        parser.parse_args(["--before", "2026-10-01", "--days", "5"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--before", "1.10.2026"])


def test_dry_run_print_lists_would_remove(root, no_harness_paths, capsys):
    run = _old_dir(root)
    _run(_args(dry_run=True, print_files=True), _Conn())
    out = capsys.readouterr().out
    assert f"would remove {run / 'out.txt'}" in out
    assert out.rstrip().endswith("dry-run: run_dirs=1 thread_dirs=0 bytes=10 kept=")
    assert run.exists()


def test_print_lists_removed(root, no_harness_paths, capsys):
    run = _old_dir(root)
    _run(_args(print_files=True), _Conn())
    assert f"removed {run / 'out.txt'}" in capsys.readouterr().out
    assert not run.exists()


def test_summary_line_format(root, capsys):
    def runs(conn, *, report, **kw):
        report.run_dirs, report.bytes = 2, 30
        report.kept["open"] += 1
        report.kept["permission"] += 2

    with patch("agento.framework.artifacts_retention.prune_run_dirs", side_effect=runs), \
         patch("agento.framework.artifacts_retention.prune_thread_dirs"):
        _run(_args(), _Conn())
    assert capsys.readouterr().out == \
        "run_dirs=2 thread_dirs=0 bytes=30 kept=open:1,permission:2\n"


def test_second_instance_exits_when_lock_held(root, capsys):
    conn = _Conn(got_lock=False)
    with patch("agento.framework.artifacts_retention.prune_run_dirs") as runs:
        _run(_args(), conn)
    assert capsys.readouterr().out == "another artifacts:prune is running\n"
    runs.assert_not_called()
    assert not any("RELEASE_LOCK" in s for s in conn.sql)
    assert conn.closed


def test_sets_read_committed_before_prune(root):
    conn = _Conn()
    with patch("agento.framework.artifacts_retention.prune_run_dirs",
               side_effect=lambda c, **kw: c.sql.append("PRUNE")):
        _run(_args(), conn)
    iso = conn.sql.index("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
    assert iso < conn.sql.index("PRUNE")
    assert any("RELEASE_LOCK" in s for s in conn.sql) and conn.closed


def test_days_zero_keeps_run_and_thread_dirs(root, no_harness_paths, capsys):
    run = _old_dir(root)
    thread = root / "_threads" / ("a" * 32)
    thread.mkdir(parents=True)
    _run(_args(days=0), _Conn())
    assert run.exists() and thread.exists()
    assert "run_dirs=0 thread_dirs=0" in capsys.readouterr().out
