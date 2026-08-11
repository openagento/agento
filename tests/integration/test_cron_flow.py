"""Integration: Cron sync → publish → execute (real MySQL, mocked Jira + Claude + crontab)."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import httpx
import respx

from agento.framework.consumer import Consumer
from agento.modules.jira.src.channel import publish_cron
from agento.modules.jira.src.toolbox_client import ToolboxClient
from agento.modules.jira_periodic_tasks.src.crontab import MARKER_BEGIN, MARKER_END, CrontabManager
from agento.modules.jira_periodic_tasks.src.sync import JiraCronSync

from .conftest import fetch_all_jobs, fetch_all_schedules, fetch_job


class TestCronSync:

    @respx.mock
    def test_cron_sync_creates_schedules_and_crontab(
        self, int_config, int_periodic_config, int_db_config, jira_cykliczne_fixture
    ):
        """Sync fetches Cykliczne issues, writes crontab entries with ENVLOAD, upserts schedules."""
        # Mock Jira
        respx.post("http://toolbox:3001/api/jira/search").mock(
            return_value=httpx.Response(200, json=jira_cykliczne_fixture)
        )

        # Mock crontab subprocess: empty current, capture new
        applied_crontab = {}

        def mock_subprocess_run(cmd, **kwargs):
            if cmd == ["crontab", "-l"]:
                return MagicMock(returncode=0, stdout="")
            if cmd == ["crontab", "-"]:
                applied_crontab["content"] = kwargs.get("input", "")
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        toolbox = ToolboxClient(int_config.toolbox_url)
        logger = logging.getLogger("test")

        with patch("agento.modules.jira_periodic_tasks.src.crontab.subprocess.run", side_effect=mock_subprocess_run):
            crontab_mgr = CrontabManager()
            syncer = JiraCronSync(int_config, int_periodic_config, toolbox, crontab_mgr, logger, db_config=int_db_config)
            crontab_mgr.apply_managed(syncer.sync_view())

        # Assert crontab content
        content = applied_crontab["content"]
        assert MARKER_BEGIN in content
        assert MARKER_END in content

        # AI-2: Co 5min — should have ENVLOAD prefix
        assert "*/5 * * * * set -a; source /opt/cron-agent/env; set +a; cd /workspace" in content
        assert "publish jira-cron AI-2" in content

        # AI-3: 1x dziennie o 8:00
        assert "0 8 * * * set -a; source /opt/cron-agent/env; set +a; cd /workspace" in content
        assert "publish jira-cron AI-3" in content

        # AI-4 (null frequency) and AI-5 (unknown frequency) should NOT appear
        assert "AI-4" not in content
        assert "AI-5" not in content

        # Assert schedules table
        schedules = fetch_all_schedules()
        assert len(schedules) == 2

        by_key = {s["issue_key"]: s for s in schedules}
        assert by_key["AI-2"]["cron_expr"] == "*/5 * * * *"
        assert by_key["AI-2"]["enabled"] == 1
        assert by_key["AI-3"]["cron_expr"] == "0 8 * * *"
        assert by_key["AI-3"]["enabled"] == 1

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
