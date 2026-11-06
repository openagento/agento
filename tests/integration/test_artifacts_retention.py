"""Retention against MySQL: the prune waits for the thread (or job) row lock that a
publisher (or a retry) holds, and the re-check under it sees what it committed."""
from __future__ import annotations

import stat
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from agento.framework import artifacts_retention
from agento.framework.artifacts_dir import prepare_thread_dir
from agento.framework.artifacts_retention import PruneReport, prune_run_dirs, prune_thread_dirs
from agento.framework.job_models import AgentType
from agento.framework.publisher import insert_job

from .conftest import _test_connection

NOW = datetime.now(UTC).replace(tzinfo=None)
_keys = iter(range(10**6))


@pytest.fixture
def conn():
    c = _test_connection(autocommit=False)
    yield c
    c.close()


@pytest.fixture
def view(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES ('t_ws', 't')")
        cur.execute("SELECT id FROM workspace WHERE code = 't_ws'")
        ws = cur.fetchone()["id"]
        cur.execute("INSERT IGNORE INTO agent_view (workspace_id, code, label) "
                    "VALUES (%s, 't_av', 't')", (ws,))
        cur.execute("SELECT id FROM agent_view WHERE code = 't_av'")
        view_id = cur.fetchone()["id"]
    conn.commit()
    return view_id


def _insert(cur, view: int) -> int:
    return insert_job(cur, agent_type=AgentType.TODO, source="jira", agent_view_id=view,
                      priority=50, reference_id="AI-1", idempotency_key=f"r:{next(_keys)}",
                      max_attempts=3, requester=None, thread="t-1")


def _old_ended_thread(conn, view: int) -> str:
    """A thread whose only job ended 40 days ago; returns its key."""
    with conn.cursor() as cur:
        job_id = _insert(cur, view)
        cur.execute("UPDATE job SET status = 'SUCCESS', finished_at = %s WHERE id = %s",
                    (NOW - timedelta(days=40), job_id))
        cur.execute("SELECT t.`key` FROM thread t JOIN job j ON j.thread_id = t.id "
                    "WHERE j.id = %s", (job_id,))
        key = cur.fetchone()["key"]
    conn.commit()
    return key


def _read_committed():
    c = _test_connection(autocommit=False)
    with c.cursor() as cur:
        cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
    return c


def test_prune_waits_for_thread_lock_and_rechecks(conn, view, tmp_path):
    key = _old_ended_thread(conn, view)
    thread_dir = tmp_path / "_threads" / key
    thread_dir.mkdir(parents=True)

    with conn.cursor() as cur:   # a publisher inserts into the thread: the row lock is held
        _insert(cur, view)

    pruner, done = _read_committed(), threading.Event()

    def prune():
        try:
            prune_thread_dirs(pruner, cutoff=NOW - timedelta(days=30), dry_run=False, report=PruneReport(),
                              root=tmp_path)
        finally:
            done.set()

    worker = threading.Thread(target=prune)
    worker.start()
    try:
        assert not done.wait(1.0), "prune did not wait for the thread lock"
        conn.commit()
        assert done.wait(10)
    finally:
        worker.join(10)
        pruner.close()
    assert thread_dir.exists()


def test_new_job_after_prune_gets_fresh_shared_dir(conn, view, tmp_path):
    key = _old_ended_thread(conn, view)
    run_dir = tmp_path / "acme" / "dev" / "1"
    run_dir.mkdir(parents=True)
    with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path)):
        prepare_thread_dir(key, run_dir)
        (tmp_path / "_threads" / key / "shared" / "old.txt").write_text("old")

        pruner = _read_committed()
        try:
            report = PruneReport()
            prune_thread_dirs(pruner, cutoff=NOW - timedelta(days=30), dry_run=False, report=report, root=tmp_path)
        finally:
            pruner.close()
        assert report.thread_dirs == 1 and not (tmp_path / "_threads" / key).exists()

        prepare_thread_dir(key, run_dir)
    shared = tmp_path / "_threads" / key / "shared"
    downloads = tmp_path / "_threads" / key / "downloads"
    assert list(shared.iterdir()) == []
    assert stat.S_IMODE(shared.stat().st_mode) == 0o2770
    assert stat.S_IMODE(downloads.stat().st_mode) == 0o2750


def test_run_dir_prune_waits_for_job_lock_and_keeps_a_retried_job(conn, view, tmp_path):
    """A retry that sets an old ended job back to TODO, committed while the prune waits on
    the job row lock, keeps the job's run dir."""
    with conn.cursor() as cur:
        job_id = _insert(cur, view)
        cur.execute("UPDATE job SET status = 'FAILED', finished_at = %s WHERE id = %s",
                    (NOW - timedelta(days=40), job_id))
    conn.commit()
    run_dir = tmp_path / "t_ws" / "t_av" / str(job_id)
    run_dir.mkdir(parents=True)

    pruner, done, report = _read_committed(), threading.Event(), PruneReport()
    with conn.cursor() as cur:   # the retry holds the row lock first
        cur.execute("SELECT id FROM job WHERE id = %s FOR UPDATE", (job_id,))
        cur.execute("UPDATE job SET status = 'TODO', finished_at = NULL WHERE id = %s", (job_id,))

    def prune():
        try:
            with patch("agento.framework.harness.find_harness", return_value=object()), \
                 patch("agento.framework.harness.persistent_home_paths_for", return_value=[]):
                prune_run_dirs(pruner, cutoff=NOW - timedelta(days=30), dry_run=False, report=report,
                               root=tmp_path)
        finally:
            done.set()

    worker = threading.Thread(target=prune)
    with patch("agento.framework.artifacts_retention._job_rows",
               wraps=artifacts_retention._job_rows) as rows:
        # The batch read sees FAILED (READ COMMITTED, the TODO is not committed yet).
        worker.start()
        try:
            assert not done.wait(1.0), "prune did not wait for the job row lock"
            conn.commit()
            assert done.wait(10)
        finally:
            worker.join(10)
            pruner.close()
    assert rows.call_count == 2
    assert run_dir.exists() and report.kept["open"] == 1
