"""Whether a periodic task run continues the task's previous session.

`jira_periodic_tasks/resume_session` is the same choice as `jira/resume_session`, for the
cron jobs this module schedules on a periodic issue (see the jira module's
`src/session_policy.py`).
"""
from __future__ import annotations

from agento.framework.job_models import AgentType
from agento.modules.jira.src.session_policy import ResumeSessionPolicy


class PeriodicResumeSessionPolicy(ResumeSessionPolicy):
    CONFIG_PATH = "jira_periodic_tasks/resume_session"
    TYPES = (AgentType.CRON.value,)
