"""`jira/resume_session` and `jira_periodic_tasks/resume_session`: a fresh session on request."""
from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.events import JobClaimBeforeEvent
from agento.modules.jira.src.session_policy import ResumeSessionPolicy, resume_session_on
from agento.modules.jira_periodic_tasks.src.session_policy import PeriodicResumeSessionPolicy

P = "agento.modules.jira.src.session_policy"


def _claim(policy, job_row, *, resume=True):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = job_row
    event = JobClaimBeforeEvent(job_id=10)
    with (patch(f"{P}.pooled", return_value=nullcontext(conn)),
          patch(f"{P}.DatabaseConfig"),
          patch(f"{P}.resume_session_on", return_value=resume) as read):
        policy().execute(event)
    return event, read


@pytest.mark.parametrize(("policy", "job_type", "path"), [
    (ResumeSessionPolicy, "todo", "jira/resume_session"),
    (ResumeSessionPolicy, "followup", "jira/resume_session"),
    (PeriodicResumeSessionPolicy, "cron", "jira_periodic_tasks/resume_session"),
])
@pytest.mark.parametrize(("resume", "fresh"), [(True, False), (False, True)])
def test_the_field_of_the_owning_module_decides_for_the_jobs_view(policy, job_type, path,
                                                                    resume, fresh):
    event, read = _claim(policy, {"type": job_type, "source": "jira", "agent_view_id": 3},
                         resume=resume)

    assert event.fresh_session is fresh
    assert event.detach_thread is False                 # the job stays in its thread
    assert (read.call_args.args[1], read.call_args.kwargs) == (path, {"agent_view_id": 3})


@pytest.mark.parametrize(("policy", "row"), [
    (ResumeSessionPolicy, None),
    (ResumeSessionPolicy, {"type": "todo", "source": "outlook", "agent_view_id": 3}),
    (ResumeSessionPolicy, {"type": "cron", "source": "jira", "agent_view_id": 3}),
    (PeriodicResumeSessionPolicy, {"type": "todo", "source": "jira", "agent_view_id": 3}),
])
def test_a_job_the_field_does_not_govern_is_left_alone(policy, row):
    event, read = _claim(policy, row, resume=False)

    assert event.fresh_session is False
    read.assert_not_called()


@pytest.mark.parametrize(("value", "on"), [
    (None, True), ("1", True), ("True", True), ("yes", True), ("0", False), ("false", False),
])
def test_the_field_is_on_unless_set_off(value, on):
    with patch(f"{P}.ScopedConfigService") as svc:
        svc.return_value.get.return_value = value
        assert resume_session_on(MagicMock(), "jira/resume_session", agent_view_id=3) is on
    assert svc.call_args.kwargs == {"strict": True}     # an unreadable level raises


def test_an_unreadable_field_raises_so_the_claim_defers_the_job():
    with (patch(f"{P}.ScopedConfigService", side_effect=LookupError("agent_view 3 not found")),
          pytest.raises(LookupError)):
        resume_session_on(MagicMock(), "jira/resume_session", agent_view_id=3)
