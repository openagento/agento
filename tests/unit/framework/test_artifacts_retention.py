"""Retention of run dirs and thread dirs: what goes, what stays, and that a session is never
lost — copy-back first, never through a link, and a dir whose content state/ cannot take
is kept."""
from __future__ import annotations

import dataclasses
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from agento.framework import artifacts_retention, persistent_home
from agento.framework.artifacts_retention import PruneReport, prune_run_dirs, prune_thread_dirs

pytestmark = pytest.mark.usefixtures("builtin_harnesses")

NOW = datetime(2026, 11, 6, 3, 23)
KEY = "0123456789abcdef0123456789abcdef"


class FakeConn:
    """Answers the queries retention makes; records commits and rollbacks."""

    def __init__(self, jobs=None, threads=None, iso="READ-COMMITTED"):
        self.jobs = jobs or {}
        self.threads = threads or {}   # key -> (open_jobs, last_finished) or None (no row)
        self.iso = iso
        self.commits = self.rollbacks = 0
        self.queries: list[str] = []

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Cur:
    def __init__(self, conn):
        self.conn, self.result = conn, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=()):
        self.conn.queries.append(sql)
        if "@@transaction_isolation" in sql:
            self.result = [{"iso": self.conn.iso}]
        elif sql.startswith("SELECT j.id"):
            self.result = [{"id": i, "resumed_session_id": None, "session_id": None,
                            "agent_type": "claude", **self.conn.jobs[i]}
                           for i in args if i in self.conn.jobs]
        elif "FROM thread WHERE `key`" in sql:
            self._key = args[0]
            self.result = [] if self.conn.threads.get(args[0]) is None else [{"id": 1}]
        else:
            open_jobs, last = self.conn.threads[self._key]
            self.result = [{"open_jobs": open_jobs, "last_finished": last}]

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result


def _write(path: Path, text: str = "x", mtime: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _age(path: Path, days: int) -> None:
    ts = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (ts, ts), follow_symlinks=False)


@pytest.fixture
def env(tmp_path):
    root, state = tmp_path / "artifacts", tmp_path / "state"
    state.mkdir()
    with patch("agento.framework.run_preparation.state_root_for_codes", return_value=state):
        yield root, state


def _run(root, name="7"):
    run = root / "acme" / "dev" / name
    run.mkdir(parents=True)
    return run


def _prune(conn, root, *, days=30, dry_run=False, **kw):
    report = PruneReport()
    prune_run_dirs(conn, cutoff=NOW - timedelta(days=days), dry_run=dry_run, report=report, root=root, **kw)
    return report


def _ended(days, status="SUCCESS", **row):
    return {"status": status, "finished_at": NOW - timedelta(days=days), **row}


class TestRunDirs:
    @pytest.mark.parametrize(("age", "removed"), [(31, True), (29, False)])
    def test_prunes_terminal_job_older_than_days(self, env, age, removed):
        root, _ = env
        run = _run(root)
        report = _prune(FakeConn({7: _ended(age)}), root)
        assert run.exists() is not removed
        assert report.run_dirs == int(removed)

    @pytest.mark.parametrize("status", ["TODO", "RUNNING", "PAUSED"])
    def test_keeps_open_job_dir_even_if_old(self, env, status):
        root, _ = env
        run = _run(root)
        _age(run, 365)
        report = _prune(FakeConn({7: {"status": status, "finished_at": None}}), root)
        assert run.exists() and report.kept["open"] == 1

    @pytest.mark.parametrize(("age", "removed"), [(31, True), (1, False)])
    def test_no_row_uses_mtime(self, env, age, removed):
        root, _ = env
        run = _run(root, "agento-run-xyz")
        _age(run, age)
        _prune(FakeConn(), root)
        assert run.exists() is not removed

    def test_pre_1_0_symlink_into_state_keeps_state(self, env):
        root, state = env
        _write(state / ".claude/projects/a/s1.jsonl", "s1")
        run = _run(root)
        (run / ".claude").mkdir()
        (run / ".claude/projects").symlink_to(state / ".claude/projects")
        _prune(FakeConn({7: _ended(40)}), root)
        assert not run.exists()
        assert (state / ".claude/projects/a/s1.jsonl").read_text() == "s1"

    def test_copy_back_before_remove(self, env):
        root, state = env
        _write(state / ".claude/projects/a/s1.jsonl", "old", 1_000_000_000)
        run = _run(root)
        _write(run / ".claude/projects/a/s1.jsonl", "newer", 2_000_000_000)
        _prune(FakeConn({7: _ended(40, session_id="s1")}), root)
        assert not run.exists()
        assert (state / ".claude/projects/a/s1.jsonl").read_text() == "newer"

    def test_relocated_session_recovered_to_original_path(self, env):
        root, state = env
        _write(state / ".claude/projects/old/s1.jsonl", "turn1", 1_000_000_000)
        run = _run(root)
        _write(run / ".claude/projects/new/s1.jsonl", "turn1+turn2", 2_000_000_000)
        _prune(FakeConn({7: _ended(40, session_id="s1")}), root)
        assert (state / ".claude/projects/old/s1.jsonl").read_text() == "turn1+turn2"
        assert not (state / ".claude/projects/new").exists()

    def test_copy_back_failure_keeps_dir_and_next_sweep_completes(self, env, monkeypatch):
        root, state = env
        _write(state / ".claude/projects/a/s1.jsonl", "old", 1_000_000_000)
        run = _run(root)
        _write(run / ".claude/projects/a/s1.jsonl", "complete", 2_000_000_000)
        conn = FakeConn({7: _ended(40, session_id="s1")})
        real_copy2 = shutil.copy2

        def half_then_fail(src, dst, **kw):
            Path(dst).write_text("comp")
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(persistent_home.shutil, "copy2", half_then_fail)
        report = _prune(conn, root)
        assert run.exists() and report.kept["copy_back_failed"] == 1
        assert (state / ".claude/projects/a/s1.jsonl").read_text() == "old"

        monkeypatch.setattr(persistent_home.shutil, "copy2", real_copy2)
        _prune(conn, root)
        assert not run.exists()
        assert (state / ".claude/projects/a/s1.jsonl").read_text() == "complete"

    def test_no_row_dir_never_duplicates_a_known_session(self, env):
        root, state = env
        _write(state / ".claude/projects/old/s1.jsonl", "same")
        run = _run(root, "agento-run-1")
        _write(run / ".claude/projects/new/s1.jsonl", "same")
        _write(run / ".claude/projects/new/s2.jsonl", "s2")
        _age(run, 40)
        _prune(FakeConn(), root)
        assert not run.exists()
        assert not (state / ".claude/projects/new/s1.jsonl").exists()
        assert (state / ".claude/projects/new/s2.jsonl").read_text() == "s2"

    @pytest.mark.parametrize(("run_text", "state_text", "state_mtime"), [
        ("longer content", "short", None),
        ("abcd", "wxyz", 1_500_000_000),
        ("abcd", "wxyz", 2_500_000_000),
    ])
    def test_no_row_dir_with_different_known_session_is_kept(self, env, run_text, state_text,
                                                             state_mtime):
        root, state = env
        _write(state / ".claude/projects/old/s1.jsonl", state_text, state_mtime)
        run = _run(root, "agento-run-1")
        _write(run / ".claude/projects/new/s1.jsonl", run_text, 1_500_000_000)
        _age(run, 40)
        report = _prune(FakeConn(), root)
        assert run.exists() and report.kept["unsaved"] == 1
        assert (state / ".claude/projects/old/s1.jsonl").read_text() == state_text
        assert not (state / ".claude/projects/new").exists()

    def test_two_no_row_dirs_with_one_session_keep_one_copy(self, env):
        """The name index is rebuilt after a save, so the second dir sees the first's copy."""
        root, state = env
        first, second = _run(root, "agento-run-1"), _run(root, "agento-run-2")
        _write(first / ".claude/projects/a/s1.jsonl", "same")
        _write(second / ".claude/projects/b/s1.jsonl", "same")
        _age(first, 40)
        _age(second, 40)
        _prune(FakeConn(), root)
        assert not first.exists() and not second.exists()
        assert sorted(p.name for p in state.rglob("s1.jsonl")) == ["s1.jsonl"]

    def test_partial_save_keeps_the_index_current(self, env):
        """A dir kept for a conflict may still have saved a session: the next row-less dir
        sees that copy (no second copy of s1)."""
        root, state = env
        _write(state / ".claude/projects/old/x.jsonl", "state x")
        first, second = _run(root, "agento-run-1"), _run(root, "agento-run-2")
        _write(first / ".claude/projects/a/s1.jsonl", "same")
        _write(first / ".claude/projects/a/x.jsonl", "run x")
        _write(second / ".claude/projects/b/s1.jsonl", "same")
        _age(first, 40)
        _age(second, 40)
        report = _prune(FakeConn(), root)
        assert first.exists() and report.kept["unsaved"] == 1
        assert not second.exists()
        assert len(list(state.rglob("s1.jsonl"))) == 1

    def test_no_row_dir_with_two_basename_matches_is_kept(self, env):
        root, state = env
        _write(state / ".claude/projects/a/s1.jsonl", "same")
        _write(state / ".claude/projects/b/s1.jsonl", "same")
        run = _run(root, "agento-run-1")
        _write(run / ".claude/projects/new/s1.jsonl", "same")
        _age(run, 40)
        assert _prune(FakeConn(), root).kept["unsaved"] == 1
        assert run.exists()

    def test_conflict_dir_at_file_path_keeps_run_dir(self, env):
        root, state = env
        _write(state / ".codex/history.jsonl/odd")
        run = _run(root)
        _write(run / ".codex/history.jsonl", "h")
        report = _prune(FakeConn({7: _ended(40, agent_type="codex")}), root)
        assert run.exists() and report.kept["unsaved"] == 1

    def test_batches_are_bounded_and_processed_before_the_next(self, env, monkeypatch):
        """CODE-8: at most _BATCH run-dir entries are listed and _BATCH ids read before a
        batch's dirs go — memory does not grow with the number of run dirs."""
        root, _ = env
        monkeypatch.setattr("agento.framework.artifacts_retention._BATCH", 2)
        runs = [_run(root, str(i)) for i in range(1, 6)]
        view = runs[0].parent
        conn = FakeConn({i: _ended(40) for i in range(1, 6)})
        events: list[str] = []
        real_rows, real_scandir = artifacts_retention._job_rows, os.scandir
        listed: list[str] = []

        class _Counting:
            def __init__(self, path):
                self.inner = real_scandir(path)
                self.path = None if isinstance(path, int) else Path(path)   # rmtree passes an fd

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.inner.close()

            def __iter__(self):
                return self

            def __next__(self):
                entry = next(self.inner)
                if self.path == view:
                    listed.append(entry.name)
                return entry

            def close(self):
                self.inner.close()

        def rows(c, ids, *, lock=False):
            if not lock:
                events.append(f"read {len(ids)} after {len(listed)} listed")
            return real_rows(c, ids, lock=lock)

        monkeypatch.setattr(artifacts_retention, "_job_rows", rows)
        monkeypatch.setattr(artifacts_retention.os, "scandir", _Counting)
        _prune(conn, root, on_file=lambda p: None)
        assert not any(r.exists() for r in runs)
        assert events == ["read 2 after 2 listed", "read 2 after 4 listed", "read 1 after 5 listed"]

    def test_status_is_rechecked_under_the_job_row_lock(self, env):
        """A job retried after the batch read (now TODO) keeps its dir."""
        root, _ = env
        run = _run(root)
        conn = FakeConn({7: _ended(40)})
        real_execute = _Cur.execute

        def execute(cur, sql, args=()):
            if "FOR UPDATE OF j" in sql:
                conn.jobs[7] = {"status": "TODO", "finished_at": None}
            real_execute(cur, sql, args)

        with patch.object(_Cur, "execute", execute):
            report = _prune(conn, root)
        assert run.exists() and report.kept["open"] == 1 and conn.rollbacks == 1

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_unknown_harness_keeps_run_dir(self, env, dry_run):
        """A dry run reports the same kept dir as a real run."""
        root, _ = env
        run = _run(root)
        report = _prune(FakeConn({7: _ended(40, agent_type="gone")}), root, dry_run=dry_run)
        assert run.exists() and report.kept["unknown_harness"] == 1 and report.run_dirs == 0

    def test_no_row_and_no_registry_keeps_run_dir(self, env):
        """With no harness registered the paths to save are unknown: keep, never drop."""
        root, _ = env
        run = _run(root, "orphan")
        _age(run, 40)
        with patch("agento.framework.harness.list_harnesses", return_value=[]):
            report = _prune(FakeConn({}), root)
        assert run.exists() and report.kept["unknown_harness"] == 1

    def test_permission_error_skipped(self, env):
        root, _ = env
        runs = [_run(root, "7"), _run(root, "8")]
        conn = FakeConn({7: _ended(40), 8: _ended(40)})
        real = shutil.rmtree

        def deny_first(path, *a, **kw):
            if Path(path).name == "7":
                raise PermissionError(13, "denied", str(path))
            real(path, *a, **kw)

        with patch("agento.framework.artifacts_retention.shutil.rmtree", side_effect=deny_first):
            report = _prune(conn, root)
        assert runs[0].exists() and not runs[1].exists()
        assert report.kept["permission"] == 1 and report.run_dirs == 1

    def test_dry_run_removes_nothing_and_reports_each_file(self, env):
        root, state = env
        run = _run(root)
        _write(run / "out/a.txt", "aa")
        _write(run / ".claude/projects/new/s9.jsonl", "s9")
        seen: list[Path] = []
        report = _prune(FakeConn({7: _ended(40)}), root, dry_run=True, on_file=seen.append)
        assert sorted(seen) == sorted([run / "out/a.txt", run / ".claude/projects/new/s9.jsonl"])
        assert run.exists() and not list(state.rglob("*.jsonl"))
        assert report.run_dirs == 1 and report.bytes == 4

    def test_report_holds_no_paths(self, env):
        assert {f.type for f in dataclasses.fields(PruneReport)} <= {"int", "Counter[str]"}
        root, _ = env
        run = _run(root)
        for i in range(2000):
            _write(run / "out" / f"{i}.txt")
        report = _prune(FakeConn({7: _ended(40)}), root)
        assert report.run_dirs == 1 and report.bytes == 2000

    def test_skips_underscore_dirs(self, env):
        root, _ = env
        for name in ("_threads", "_fallback"):
            d = root / name / "dev" / "7"
            d.mkdir(parents=True)
            _age(d, 400)
        _prune(FakeConn(), root)
        assert (root / "_threads/dev/7").exists() and (root / "_fallback/dev/7").exists()

    def test_batches_job_lookup(self, env):
        root, _ = env
        for i in range(1, 1201):
            (root / "acme" / "dev" / str(i)).mkdir(parents=True)
        conn = FakeConn({i: _ended(1) for i in range(1, 1201)})
        _prune(conn, root)
        assert sum(q.startswith("SELECT j.id") for q in conn.queries) == 3


def _thread(root: Path, key: str = KEY) -> Path:
    d = root / "_threads" / key
    _write(d / "shared" / "f.txt")
    return d


def _prune_threads(conn, root, *, days=30, dry_run=False, **kw):
    report = PruneReport()
    prune_thread_dirs(conn, cutoff=NOW - timedelta(days=days), dry_run=dry_run, report=report, root=root, **kw)
    return report


class TestThreadDirs:
    def test_thread_dir_removed_when_all_jobs_old_and_ended(self, tmp_path):
        d = _thread(tmp_path)
        conn = FakeConn(threads={KEY: (0, NOW - timedelta(days=31))})
        assert _prune_threads(conn, tmp_path).thread_dirs == 1
        assert not d.exists() and conn.commits == 1

    def test_thread_dir_kept_with_open_job(self, tmp_path):
        d = _thread(tmp_path)
        report = _prune_threads(FakeConn(threads={KEY: (1, NOW - timedelta(days=99))}), tmp_path)
        assert d.exists() and report.kept["open"] == 1

    def test_thread_dir_kept_when_last_job_recent(self, tmp_path):
        d = _thread(tmp_path)
        _prune_threads(FakeConn(threads={KEY: (0, NOW - timedelta(days=2))}), tmp_path)
        assert d.exists()

    @pytest.mark.parametrize(("age", "removed"), [(31, True), (1, False)])
    def test_thread_dir_without_row_uses_mtime(self, tmp_path, age, removed):
        d = _thread(tmp_path)
        _age(d, age)
        _prune_threads(FakeConn(), tmp_path)
        assert d.exists() is not removed

    def test_thread_dir_symlink_entry_not_followed(self, tmp_path):
        outside = _write(tmp_path / "outside" / "keep.txt", "keep")
        _write(tmp_path / "artifacts" / "_threads" / "f" / "x")  # not a key: skipped
        d = _thread(tmp_path / "artifacts")
        (d / "link").symlink_to(outside.parent)
        (tmp_path / "artifacts" / "_threads" / ("f" * 32)).symlink_to(outside.parent)
        _prune_threads(FakeConn(threads={KEY: (0, NOW - timedelta(days=40))}), tmp_path / "artifacts")
        assert not d.exists()
        assert outside.read_text() == "keep"

    def test_dry_run_rolls_back(self, tmp_path):
        d = _thread(tmp_path)
        conn = FakeConn(threads={KEY: (0, NOW - timedelta(days=40))})
        _prune_threads(conn, tmp_path, dry_run=True)
        assert d.exists() and conn.rollbacks == 1 and conn.commits == 0

    def test_refuses_repeatable_read_connection(self, tmp_path):
        _thread(tmp_path)
        with pytest.raises(RuntimeError, match="READ COMMITTED"):
            _prune_threads(FakeConn(iso="REPEATABLE-READ"), tmp_path)
