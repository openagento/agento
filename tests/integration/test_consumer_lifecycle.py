"""Integration: Consumer retry, dead-letter, concurrent dequeue, resume (real MySQL)."""
from __future__ import annotations

import logging
import subprocess
from unittest.mock import patch

from agento.framework.consumer import Consumer
from agento.modules.claude.src.output_parser import ClaudeResult
from agento.modules.claude.src.runner import ClaudeSubprocessRunner

from .conftest import fetch_job, insert_primary_token, insert_queued_job, update_job


class TestRetryFlow:

    def test_retryable_error_requeues_with_backoff(self, int_db_config, int_consumer_config):
        """RuntimeError → requeue with backoff → retry → succeed."""
        insert_primary_token("claude")
        logger = logging.getLogger("test")
        job_id = insert_queued_job(reference_id="AI-1", idempotency_key="retry:1")

        # Attempt 1: fail with RuntimeError
        with patch.object(ClaudeSubprocessRunner, "execute", side_effect=RuntimeError("Claude timeout")):
            consumer = Consumer(int_db_config, int_consumer_config, logger)
            job = consumer._try_dequeue()
            assert job is not None
            consumer._execute_job(job)

        # Assert: requeued with backoff
        row = fetch_job(job_id)
        assert row["status"] == "TODO"
        assert row["attempt"] == 1
        assert row["error_class"] == "RuntimeError"
        assert row["scheduled_after"] > row["updated_at"]

        # Move scheduled_after to past so consumer can dequeue again
        update_job(job_id, scheduled_after="2000-01-01 00:00:00")

        # Attempt 2: succeed
        success = ClaudeResult(
            raw_output="ok", input_tokens=100, output_tokens=50,
            cost_usd=0.01, num_turns=2, duration_ms=3000, session_id="success",
        )
        with patch.object(ClaudeSubprocessRunner, "execute", return_value=success):
            consumer2 = Consumer(int_db_config, int_consumer_config, logger)
            job2 = consumer2._try_dequeue()
            assert job2 is not None
            assert job2.id == job_id
            consumer2._execute_job(job2)

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS"
        assert row["attempt"] == 2

    def test_non_retryable_error_dead_letters(self, int_db_config, int_consumer_config):
        """ValueError (non-retryable) → immediate DEAD status."""
        insert_primary_token("claude")
        logger = logging.getLogger("test")
        job_id = insert_queued_job(reference_id="AI-2", idempotency_key="dead:1")

        with patch.object(ClaudeSubprocessRunner, "execute", side_effect=ValueError("Bad prompt")):
            consumer = Consumer(int_db_config, int_consumer_config, logger)
            job = consumer._try_dequeue()
            assert job is not None
            consumer._execute_job(job)

        row = fetch_job(job_id)
        assert row["status"] == "DEAD"
        assert row["error_class"] == "ValueError"
        assert "Bad prompt" in row["error_message"]

    def test_max_attempts_exhausted_dead_letters(self, int_db_config, int_consumer_config):
        """Job with max_attempts=1 is dead-lettered after first failure."""
        insert_primary_token("claude")
        logger = logging.getLogger("test")
        job_id = insert_queued_job(
            reference_id="AI-3", idempotency_key="maxed:1", max_attempts=1,
        )

        with patch.object(ClaudeSubprocessRunner, "execute", side_effect=RuntimeError("fail")):
            consumer = Consumer(int_db_config, int_consumer_config, logger)
            job = consumer._try_dequeue()
            assert job is not None
            consumer._execute_job(job)

        row = fetch_job(job_id)
        assert row["status"] == "DEAD"


class TestConcurrentDequeue:

    def test_sequential_dequeue_claims_different_jobs(self, int_db_config, int_consumer_config):
        """Two sequential dequeues claim different jobs (CLAIM_SQL sets status=RUNNING)."""
        logger = logging.getLogger("test")

        job_id_1 = insert_queued_job(reference_id="AI-10", idempotency_key="conc:1")
        job_id_2 = insert_queued_job(reference_id="AI-11", idempotency_key="conc:2")

        consumer = Consumer(int_db_config, int_consumer_config, logger)

        # First dequeue claims job 1
        job1 = consumer._try_dequeue()
        assert job1 is not None

        # Second dequeue must get a different job (first is now RUNNING)
        job2 = consumer._try_dequeue()
        assert job2 is not None

        assert job1.id != job2.id
        assert {job1.id, job2.id} == {job_id_1, job_id_2}

        # Both should be RUNNING in DB
        row1 = fetch_job(job_id_1)
        row2 = fetch_job(job_id_2)
        assert row1["status"] == "RUNNING"
        assert row2["status"] == "RUNNING"

        # Third dequeue should return None (queue empty)
        job3 = consumer._try_dequeue()
        assert job3 is None


class TestResumeOnTimeout:

    def test_timeout_captures_session_id_then_resumes(self, int_db_config, int_consumer_config):
        """Attempt 1 times out with session_id captured -> Attempt 2 resumes."""
        insert_primary_token("claude")
        logger = logging.getLogger("test")
        job_id = insert_queued_job(
            reference_id="AI-RESUME-1",
            idempotency_key="resume:1",
            max_attempts=3,
        )

        # Attempt 1: simulate TimeoutExpired with session_id attached
        timeout_exc = subprocess.TimeoutExpired(cmd="claude", timeout=600)
        timeout_exc.session_id = "sess-timeout-abc"  # type: ignore[attr-defined]

        with patch.object(ClaudeSubprocessRunner, "execute", side_effect=timeout_exc):
            consumer = Consumer(int_db_config, int_consumer_config, logger)
            job = consumer._try_dequeue()
            assert job is not None
            consumer._execute_job(job)

        # Verify: job retried as TODO with session_id persisted
        row = fetch_job(job_id)
        assert row["status"] == "TODO"
        assert row["attempt"] == 1
        assert row["session_id"] == "sess-timeout-abc"

        # Move scheduled_after to past
        update_job(job_id, scheduled_after="2000-01-01 00:00:00")

        # Attempt 2: the resume is just an execute() whose RunRequest carries the
        # session_id — there is no separate resume() entry point any more.
        resume_result = ClaudeResult(
            raw_output="resumed ok",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.01,
            num_turns=2,
            duration_ms=3000,
            session_id="sess-timeout-abc",
        )
        with (
            patch.object(
                ClaudeSubprocessRunner, "execute", return_value=resume_result,
            ) as mock_execute,
            patch.object(Consumer, "_is_pid_alive", return_value=False),
        ):
            consumer2 = Consumer(int_db_config, int_consumer_config, logger)
            job2 = consumer2._try_dequeue()
            assert job2 is not None
            assert job2.id == job_id
            assert job2.session_id == "sess-timeout-abc"
            consumer2._execute_job(job2)

            mock_execute.assert_called_once()
            request = mock_execute.call_args.args[0]
            assert request.session_id == "sess-timeout-abc"
            assert request.model is None

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS"
        assert row["attempt"] == 2

    def test_stale_recovery_with_dead_pid(self, int_db_config, int_consumer_config):
        """RUNNING job with dead PID is recovered to TODO."""
        logger = logging.getLogger("test")
        job_id = insert_queued_job(
            reference_id="AI-STALE-1",
            idempotency_key="stale:1",
            max_attempts=3,
        )
        # Manually set to RUNNING with a dead PID
        update_job(job_id, status="RUNNING", attempt=1, pid=99999)

        consumer = Consumer(int_db_config, int_consumer_config, logger)
        consumer._recover_stale_jobs()

        row = fetch_job(job_id)
        assert row["status"] == "TODO"
        assert row["error_class"] == "StaleJobRecovery"
        assert "pid=99999" in row["error_message"]

    def test_stale_recovery_dead_pid_max_attempts(self, int_db_config, int_consumer_config):
        """RUNNING job with dead PID and max attempts exhausted -> DEAD."""
        logger = logging.getLogger("test")
        job_id = insert_queued_job(
            reference_id="AI-STALE-2",
            idempotency_key="stale:2",
            max_attempts=2,
        )
        update_job(job_id, status="RUNNING", attempt=2, pid=99999)

        consumer = Consumer(int_db_config, int_consumer_config, logger)
        consumer._recover_stale_jobs()

        row = fetch_job(job_id)
        assert row["status"] == "DEAD"
        assert row["error_class"] == "StaleJobRecovery"
