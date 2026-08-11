"""Integration: job pause/resume mid-run (real MySQL).

Reproduces the bug where a job paused mid-run (via SIGTERM + status flip)
was incorrectly finalized as SUCCESS by the consumer when the agent
subprocess exited cleanly.
"""
from __future__ import annotations

import logging
import threading
from unittest.mock import patch

import pytest

from agento.framework.consumer import Consumer, _JobResult
from agento.framework.database_config import DatabaseConfig
from agento.framework.db import get_connection
from agento.framework.harness import RunResult
from agento.framework.job_store import pause_job, resume_job

from .conftest import fetch_job, insert_primary_token, insert_queued_job, update_job


@pytest.fixture
def _redirect_env_db(int_db_config):
    """Redirect DatabaseConfig.from_env() to the test DB so observers using
    from_env() can still connect during integration tests."""
    with patch.object(DatabaseConfig, "from_env", return_value=int_db_config):
        yield


class TestPauseResumeMidRun:

    def test_pause_during_run_preserves_paused_status_and_resume_succeeds(
        self, int_db_config, int_consumer_config, _redirect_env_db,
    ):
        """Reproduces the user-reported bug: paused job ended up SUCCESS.

        A RUNNING job is paused mid-flight. The mocked agent subprocess
        exits cleanly afterwards (as the real Claude CLI does on SIGTERM).
        Consumer's finalize must honor the PAUSED status and NOT overwrite
        with SUCCESS.
        """
        insert_primary_token("claude")
        logger = logging.getLogger("test")

        job_id = insert_queued_job(reference_id="AI-PAUSE-1", idempotency_key="pause:1")

        run_started = threading.Event()
        allow_run_to_return = threading.Event()

        def fake_run_job(job):
            # Simulate pid + session_id capture (as real runner does via callbacks)
            consumer._save_pid(job.id, 99999)
            consumer._save_session_id(job.id, "sess-pause-abc")
            run_started.set()
            assert allow_run_to_return.wait(timeout=10), "test timeout"
            return _JobResult.from_run_result(
                RunResult(raw_output="ok", input_tokens=100, output_tokens=50,
                          duration_ms=3000, session_id="success"),
                summary="ok",
            )

        consumer = Consumer(int_db_config, int_consumer_config, logger)

        with patch.object(Consumer, "_run_job", side_effect=fake_run_job):
            job = consumer._try_dequeue()
            assert job is not None
            assert job.id == job_id

            worker = threading.Thread(target=consumer._execute_job, args=(job,))
            worker.start()

            assert run_started.wait(timeout=5), "run() never started"

            conn = get_connection(int_db_config)
            try:
                paused_job = pause_job(conn, job_id)
            finally:
                conn.close()
            assert paused_job.status.value == "PAUSED"

            row = fetch_job(job_id)
            assert row["status"] == "PAUSED", f"DB status after pause: {row['status']}"

            # Release the fake runner — returns "success", which used to
            # overwrite PAUSED with SUCCESS before the fix.
            allow_run_to_return.set()
            worker.join(timeout=10)
            assert not worker.is_alive()

        row = fetch_job(job_id)
        assert row["status"] == "PAUSED", (
            f"BUG: expected PAUSED after mid-run pause, got {row['status']}"
        )
        assert row["session_id"] == "sess-pause-abc"

        # ---- Resume phase ----
        conn = get_connection(int_db_config)
        try:
            resumed_job = resume_job(conn, job_id)
        finally:
            conn.close()
        assert resumed_job.status.value == "TODO"

        update_job(job_id, scheduled_after="2000-01-01 00:00:00")

        def fake_resume_run(job):
            return _JobResult.from_run_result(
                RunResult(raw_output="resumed ok", input_tokens=100, output_tokens=50,
                          duration_ms=3000, session_id="sess-pause-abc"),
                summary="resumed session_id=sess-pause-abc",
            )

        with patch.object(Consumer, "_run_job", side_effect=fake_resume_run):
            consumer2 = Consumer(int_db_config, int_consumer_config, logger)
            job2 = consumer2._try_dequeue()
            assert job2 is not None
            assert job2.id == job_id
            assert job2.session_id == "sess-pause-abc"
            consumer2._execute_job(job2)

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS"

    def test_pause_during_run_preserves_paused_status_on_error(
        self, int_db_config, int_consumer_config, _redirect_env_db,
    ):
        """Same as above but the agent raises after pause — status must remain PAUSED."""
        insert_primary_token("claude")
        logger = logging.getLogger("test")

        job_id = insert_queued_job(reference_id="AI-PAUSE-2", idempotency_key="pause:2")

        run_started = threading.Event()
        allow_run_to_return = threading.Event()

        def fake_run_job(job):
            consumer._save_pid(job.id, 99999)
            consumer._save_session_id(job.id, "sess-pause-xyz")
            run_started.set()
            allow_run_to_return.wait(timeout=10)
            raise RuntimeError("simulated agent crash after SIGTERM")

        consumer = Consumer(int_db_config, int_consumer_config, logger)

        with patch.object(Consumer, "_run_job", side_effect=fake_run_job):
            job = consumer._try_dequeue()
            assert job is not None

            worker = threading.Thread(target=consumer._execute_job, args=(job,))
            worker.start()

            assert run_started.wait(timeout=5)

            conn = get_connection(int_db_config)
            try:
                pause_job(conn, job_id)
            finally:
                conn.close()
            assert fetch_job(job_id)["status"] == "PAUSED"

            allow_run_to_return.set()
            worker.join(timeout=10)
            assert not worker.is_alive()

        row = fetch_job(job_id)
        assert row["status"] == "PAUSED", (
            f"BUG: expected PAUSED after mid-run pause (error path), got {row['status']}"
        )
