"""Integration: verify the exact prompt reaching the runner for each workflow x channel."""
from __future__ import annotations

import logging
from unittest.mock import patch

import httpx
import respx

from agento.framework.consumer import Consumer
from agento.modules.claude.src.output_parser import ClaudeResult

from .conftest import fetch_job, insert_primary_token, insert_queued_job


def _capturing_claude():
    """Context manager that captures the prompt passed to ClaudeSubprocessRunner.execute."""
    result = ClaudeResult(
        raw_output="ok",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
        num_turns=2,
        duration_ms=5000,
        session_id="success",
    )
    captured = {}

    def capture(request):
        captured["prompt"] = request.prompt
        return result

    return patch("agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute", side_effect=capture), captured


class TestPromptDelivery:
    """End-to-end: job in DB → consumer dispatch → assert prompt content."""

    @staticmethod
    def _ensure_primary_token():
        insert_primary_token("claude")

    def _dequeue_and_execute(self, db_config, consumer_config):
        self._ensure_primary_token()
        logger = logging.getLogger("test")
        consumer = Consumer(db_config, consumer_config, logger)
        job = consumer._try_dequeue()
        assert job is not None
        consumer._execute_job(job)
        return job

    # -- CRON ---------------------------------------------------------------

    def test_cron_job_delivers_jira_prompt(self, int_db_config, int_consumer_config):
        insert_queued_job(
            job_type="cron", reference_id="AI-3", idempotency_key="cron:1"
        )

        patcher, captured = _capturing_claude()
        with patcher:
            job = self._dequeue_and_execute(int_db_config, int_consumer_config)

        prompt = captured["prompt"]
        assert "jira_get_issue" in prompt
        assert "jira_add_comment" in prompt
        assert "AI-3" in prompt
        assert "Nie zmieniaj statusu" in prompt
        assert "cykliczne" in prompt.lower()

        row = fetch_job(job.id)
        assert row["status"] == "SUCCESS"

    # -- TODO (specific issue) ---------------------------------------------

    def test_todo_specific_delivers_jira_prompt(self, int_db_config, int_consumer_config):
        insert_queued_job(
            job_type="todo", reference_id="AI-10", idempotency_key="todo:1"
        )

        patcher, captured = _capturing_claude()
        with patcher:
            job = self._dequeue_and_execute(int_db_config, int_consumer_config)

        prompt = captured["prompt"]
        assert "jira_get_issue" in prompt
        assert "jira_transition_issue" in prompt
        assert "In Progress" in prompt
        assert "Review" in prompt
        assert "reporter" in prompt
        assert "AI-10" in prompt
        assert "KROK 6" in prompt

        row = fetch_job(job.id)
        assert row["status"] == "SUCCESS"

    # -- TODO (dispatch — discover + pick) ---------------------------------

    @respx.mock
    def test_todo_dispatch_delivers_prompt_for_picked_task(
        self, int_db_config, int_consumer_config, jira_todo_fixture
    ):
        respx.post("http://toolbox:3001/api/jira/search").mock(
            return_value=httpx.Response(200, json=jira_todo_fixture)
        )

        insert_queued_job(
            job_type="todo",
            reference_id=None,
            idempotency_key="todo-dispatch:1",
        )

        patcher, captured = _capturing_claude()
        with patcher:
            job = self._dequeue_and_execute(int_db_config, int_consumer_config)

        prompt = captured["prompt"]
        # Should contain the picked issue key (AI-10 = highest priority in fixture)
        assert "AI-10" in prompt
        assert "jira_get_issue" in prompt
        assert "KROK 6" in prompt

        row = fetch_job(job.id)
        assert row["status"] == "SUCCESS"
        assert row["reference_id"] == "AI-10"

    # -- FOLLOWUP ----------------------------------------------------------

    def test_followup_delivers_prompt_with_instructions(self, int_db_config, int_consumer_config):
        insert_queued_job(
            job_type="followup",
            reference_id="AI-5",
            idempotency_key="followup:1",
            context="Sprawdź czy reindeks się zakończył",
        )

        patcher, captured = _capturing_claude()
        with patcher:
            job = self._dequeue_and_execute(int_db_config, int_consumer_config)

        prompt = captured["prompt"]
        assert "jira_get_issue" in prompt
        assert "jira_add_comment" in prompt
        assert "schedule_followup" in prompt
        assert "KONTEKST" in prompt
        assert "Sprawdź czy reindeks się zakończył" in prompt
        assert "AI-5" in prompt
        assert "follow-up" in prompt.lower()

        row = fetch_job(job.id)
        assert row["status"] == "SUCCESS"
