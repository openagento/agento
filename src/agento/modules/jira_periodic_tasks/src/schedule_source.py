"""The ``jira_periodic`` schedule source (AG-67 plan §5.4): a fire publishes the issue's ``cron`` job.

The job's thread stays the issue's, ``jira:<KEY>`` (the default ref of source ``jira``), so
``CronWorkflow`` and the resume policy see what a manual ``publish jira-cron`` gives them.
"""
from __future__ import annotations

from agento.framework.agent_view_runtime import resolve_publish_priority
from agento.framework.schedule.sources import FireSpec


class JiraPeriodicSource:
    name = "jira_periodic"
    editable = False  # jira:periodic:sync owns the row; Jira is the place to change it

    def fire_spec(self, cur, row: dict) -> FireSpec:
        priority = resolve_publish_priority(cur.connection, row["agent_view_id"])
        return FireSpec("jira", "cron", row["source_ref"], None, None, None, priority)
