"""Integration: Sync schedule enable/disable lifecycle (real MySQL)."""
from __future__ import annotations

import copy
import logging

import httpx
import respx

from agento.modules.jira.src.toolbox_client import ToolboxClient
from agento.modules.jira_periodic_tasks.src.sync import JiraCronSync

from .conftest import fetch_all_schedules


class TestSyncScheduleLifecycle:

    @respx.mock
    def test_sync_upserts_and_disables_removed_schedules(
        self, int_config, int_periodic_config, int_db_config, jira_cykliczne_fixture
    ):
        """First sync: 2 issues enabled. Second sync: 1 removed → disabled."""
        logger = logging.getLogger("test")
        toolbox = ToolboxClient(int_config.toolbox_url)

        # First sync: AI-2 and AI-3 (AI-4 has null freq, AI-5 unknown — both skipped)
        respx.post("http://toolbox:3001/api/jira/search").mock(
            return_value=httpx.Response(200, json=jira_cykliczne_fixture)
        )

        syncer = JiraCronSync(
            int_config, int_periodic_config, toolbox, logger, db_config=int_db_config,
        )
        syncer.sync_view()

        schedules = fetch_all_schedules()
        assert len(schedules) == 2
        by_key = {s["issue_key"]: s for s in schedules}
        assert by_key["AI-2"]["enabled"] == 1
        assert by_key["AI-3"]["enabled"] == 1

        # Second sync: only AI-2 remains (AI-3 removed from Cykliczne)
        fixture_only_ai2 = copy.deepcopy(jira_cykliczne_fixture)
        fixture_only_ai2["issues"] = [
            i for i in fixture_only_ai2["issues"] if i["key"] == "AI-2"
        ]

        respx.post("http://toolbox:3001/api/jira/search").mock(
            return_value=httpx.Response(200, json=fixture_only_ai2)
        )

        syncer2 = JiraCronSync(
            int_config, int_periodic_config, toolbox, logger, db_config=int_db_config,
        )
        syncer2.sync_view()

        schedules = fetch_all_schedules()
        by_key = {s["issue_key"]: s for s in schedules}
        assert by_key["AI-2"]["enabled"] == 1
        assert by_key["AI-3"]["enabled"] == 0  # Disabled

    @respx.mock
    def test_sync_updates_existing_schedule_summary(self, int_config, int_periodic_config, int_db_config):
        """ON DUPLICATE KEY UPDATE: summary changes are reflected."""
        logger = logging.getLogger("test")
        toolbox = ToolboxClient(int_config.toolbox_url)

        fixture_v1 = {
            "issues": [{
                "key": "AI-2",
                "fields": {
                    "summary": "Old summary",
                    "customfield_10709": {"value": "Co 5min"},
                },
            }]
        }

        respx.post("http://toolbox:3001/api/jira/search").mock(
            return_value=httpx.Response(200, json=fixture_v1)
        )

        syncer = JiraCronSync(
            int_config, int_periodic_config, toolbox, logger, db_config=int_db_config,
        )
        syncer.sync_view()

        schedules = fetch_all_schedules()
        assert len(schedules) == 1
        assert schedules[0]["summary"] == "Old summary"

        # Update summary
        fixture_v2 = copy.deepcopy(fixture_v1)
        fixture_v2["issues"][0]["fields"]["summary"] = "New summary"

        respx.post("http://toolbox:3001/api/jira/search").mock(
            return_value=httpx.Response(200, json=fixture_v2)
        )

        syncer2 = JiraCronSync(
            int_config, int_periodic_config, toolbox, logger, db_config=int_db_config,
        )
        syncer2.sync_view()

        schedules = fetch_all_schedules()
        assert len(schedules) == 1
        assert schedules[0]["summary"] == "New summary"
