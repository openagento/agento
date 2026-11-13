"""Integration: Cron sync → publish → execute (real MySQL, mocked Jira + Claude)."""
from __future__ import annotations

import logging
import subprocess
from unittest.mock import patch

import httpx
import respx

from agento.framework.consumer import Consumer
from agento.modules.jira.src.channel import publish_cron
from agento.modules.jira.src.toolbox_client import ToolboxClient
from agento.modules.jira_periodic_tasks.src.sync import JiraCronSync

from .conftest import fetch_all_jobs, fetch_all_schedules, fetch_job


class TestCronSync:

    @respx.mock
    def test_cron_sync_upserts_schedules(
        self, int_config, int_periodic_config, int_db_config, jira_cykliczne_fixture, int_agent_view
    ):
        """Sync fetches Cykliczne issues into the ``schedule`` table and touches no crontab.

        ``scheduler:tick`` fires the rows; a sync that wrote a crontab would be writing a file
        uid ``agent`` must not control.
        """
        respx.post("http://toolbox:3001/api/jira/search").mock(
            return_value=httpx.Response(200, json=jira_cykliczne_fixture)
        )

        toolbox = ToolboxClient(int_config.toolbox_url, capability_token="test-cap")
        logger = logging.getLogger("test")

        with patch("subprocess.run", wraps=subprocess.run) as run:  # the rule validator runs a child
            syncer = JiraCronSync(
                int_config, int_periodic_config, toolbox, logger, db_config=int_db_config,
                agent_view_id=int_agent_view,
            )
            syncer.sync_view()
        assert not any("crontab" in str(c) for c in run.call_args_list)

        schedules = fetch_all_schedules()
        assert len(schedules) == 2

        by_key = {s["source_ref"]: s for s in schedules}
        assert by_key["AI-2"]["source"] == "jira_periodic"
        assert by_key["AI-2"]["rrule"] == "FREQ=HOURLY;BYMINUTE=0,5,10,15,20,25,30,35,40,45,50,55"
        assert by_key["AI-2"]["enabled"] == 1
        assert by_key["AI-3"]["rrule"] == "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"
        assert by_key["AI-3"]["enabled"] == 1
        # AI-4 (null frequency) and AI-5 (unknown frequency) never become schedules.
        assert "AI-4" not in by_key
        assert "AI-5" not in by_key

    def test_cron_publish_and_execute_end_to_end(
        self, int_db_config, int_consumer_config, mock_claude
    ):
        """Publish a cron job, consumer dequeues and executes it."""
        logger = logging.getLogger("test")

        inserted = publish_cron(int_db_config, "AI-3", logger)
        assert inserted is True

        jobs = fetch_all_jobs()
        assert len(jobs) == 1
        assert jobs[0]["status"] == "TODO"
        assert jobs[0]["type"] == "cron"
        assert jobs[0]["reference_id"] == "AI-3"

        consumer = Consumer(int_db_config, int_consumer_config, logger)
        job = consumer._try_dequeue()
        assert job is not None

        consumer._execute_job(job)

        row = fetch_job(job.id)
        assert row["status"] == "SUCCESS"
        assert row["reference_id"] == "AI-3"
        assert "session_id=" in row["result_summary"]
