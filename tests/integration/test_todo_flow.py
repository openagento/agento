"""Integration: TODO task dispatch end-to-end (real MySQL, mocked Jira + Claude)."""
from __future__ import annotations

import logging

import httpx
import respx

from agento.framework.consumer import Consumer
from agento.modules.jira.src.channel import publish_todo

from .conftest import fetch_all_jobs, fetch_job


class TestTodoDispatch:

    @respx.mock
    def test_todo_dispatch_publishes_dequeues_and_succeeds(
        self, int_db_config, int_consumer_config, mock_claude, jira_todo_fixture
    ):
        """Full flow: publish dispatch → consumer dequeues → picks task → executes → SUCCESS."""
        # Mock Jira: return TODO tasks (AI-10 High, AI-11 Critical)
        respx.post("http://toolbox:3001/api/jira/search").mock(
            return_value=httpx.Response(200, json=jira_todo_fixture)
        )

        # 1. Publish a dispatch job (no specific issue_key)
        logger = logging.getLogger("test")
        inserted = publish_todo(int_db_config, issue_key=None, logger=logger)
        assert inserted is True

        # 2. Verify TODO job in DB
        jobs = fetch_all_jobs()
        assert len(jobs) == 1
        job_id = jobs[0]["id"]
        assert jobs[0]["status"] == "TODO"
        assert jobs[0]["type"] == "todo"
        assert jobs[0]["reference_id"] is None  # dispatch — no ref yet

        # 3. Consumer dequeues
        consumer = Consumer(int_db_config, int_consumer_config, logger)
        job = consumer._try_dequeue()
        assert job is not None
        assert job.id == job_id

        # 4. Verify RUNNING in DB
        row = fetch_job(job_id)
        assert row["status"] == "RUNNING"
        assert row["attempt"] == 1

        # 5. Execute (Claude mocked) + finalize
        consumer._execute_job(job)

        # 6. Verify SUCCESS + reference_id set to highest-priority task
        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS"
        # AI-10 comes first in JQL order (ORDER BY priority DESC, created ASC)
        assert row["reference_id"] == "AI-10"
        assert row["result_summary"] is not None
        assert "session_id=" in row["result_summary"]

    @respx.mock
    def test_todo_dispatch_no_tasks_succeeds_with_summary(
        self, int_db_config, int_consumer_config, mock_claude, jira_empty_fixture
    ):
        """When Jira has no TODO tasks, dispatch job still succeeds with informative summary."""
        respx.post("http://toolbox:3001/api/jira/search").mock(
            return_value=httpx.Response(200, json=jira_empty_fixture)
        )

        logger = logging.getLogger("test")
        publish_todo(int_db_config, issue_key=None, logger=logger)

        consumer = Consumer(int_db_config, int_consumer_config, logger)
        job = consumer._try_dequeue()
        assert job is not None

        consumer._execute_job(job)

        row = fetch_job(job.id)
        assert row["status"] == "SUCCESS"
        assert row["result_summary"] == "No TODO tasks found"
