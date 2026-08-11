"""E2E tests: job inserted → consumer dequeues → workflow executes → DB verified."""
from __future__ import annotations

import logging

from agento.framework.consumer import Consumer

from .conftest import fetch_job, insert_queued_job


class TestCronJobClaude:
    """Claude provider: insert cron job → execute → verify DB state."""

    def test_cron_claude_saves_model_and_output(self, int_db_config, int_consumer_config, mock_claude):
        job_id = insert_queued_job(
            job_type="cron",
            reference_id="AI-1",
            idempotency_key="e2e:claude:1",
        )

        logger = logging.getLogger("test")
        consumer = Consumer(int_db_config, int_consumer_config, logger)
        job = consumer._try_dequeue()
        assert job is not None
        assert job.id == job_id

        consumer._execute_job(job)

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS"
        assert row["agent_type"] == "claude"
        assert row["prompt"] is not None
        assert len(row["prompt"]) > 0
        assert row["output"] is not None
        assert "session_id=success" in row["result_summary"]
        assert "in=1500" in row["result_summary"]
        assert "out=800" in row["result_summary"]
        assert "duration_ms=45000" in row["result_summary"]


class TestCronJobCodex:
    """Codex provider: insert cron job → execute → verify DB state."""

    def test_cron_codex_saves_model_and_output(self, int_db_config, int_consumer_config, mock_codex):
        job_id = insert_queued_job(
            job_type="cron",
            reference_id="AI-1",
            idempotency_key="e2e:codex:1",
        )

        logger = logging.getLogger("test")
        consumer = Consumer(int_db_config, int_consumer_config, logger)
        job = consumer._try_dequeue()
        assert job is not None

        consumer._execute_job(job)

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS"
        assert row["agent_type"] == "codex"
        assert row["model"] == "o3"
        assert row["prompt"] is not None
        assert len(row["prompt"]) > 0
        assert row["output"] is not None
        assert row["input_tokens"] == 6374
        assert "in=6374" in row["result_summary"]
