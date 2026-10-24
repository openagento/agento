"""`Workflow.execute` continues the session the consumer resolved, for every workflow."""
from __future__ import annotations

import logging

import pytest

from agento.framework.channels.test import TestChannel
from agento.framework.harness import RunRequest, RunResult
from agento.framework.job_models import AgentType, Job
from agento.framework.workflows.base import JobContext, Workflow
from agento.framework.workflows.blank import BlankWorkflow
from agento.framework.workflows.followup import FollowupWorkflow
from agento.framework.workflows.todo import TodoWorkflow
from agento.modules.jira_periodic_tasks.src.workflows.cron import CronWorkflow
from agento.modules.subagent.src.workflow import SubagentWorkflow


class _Prompted(Workflow):
    def build_prompt(self, channel, reference_id, **kwargs):
        return "only the new turn" if kwargs["resumed"] else "the whole task"


class _Runner:
    def __init__(self, found=None):
        self.requests: list[RunRequest] = []
        if found is not None:
            self.prepare_resume = lambda _sid: found

    def execute(self, request):
        self.requests.append(request)
        return RunResult(raw_output="ok")


def _context(session):
    return JobContext(config={}, logger=logging.getLogger("t"),
                      update_reference_id=lambda *a: None, resume_session_id=session)


@pytest.mark.parametrize(("session", "found", "sent", "prompt"), [
    ("s-1", True, "s-1", "only the new turn"),       # resumed
    (None, True, None, "the whole task"),            # fresh
    ("s-1", False, None, "the whole task"),          # the session is gone: fresh
    ("s-1", None, "s-1", "only the new turn"),       # a runner with no prepare_resume
])
def test_execute_resumes_or_starts_fresh(session, found, sent, prompt):
    runner = _Runner(found)
    result = _Prompted(runner, logging.getLogger("t")).execute(
        TestChannel(), "R", context=_context(session))
    [request] = runner.requests
    assert (request.session_id, request.prompt, result.prompt) == (sent, prompt, prompt)


def test_with_no_context_the_run_is_fresh():
    runner = _Runner(True)
    _Prompted(runner, logging.getLogger("t")).execute(TestChannel(), "R")
    assert runner.requests[0].session_id is None


@pytest.mark.parametrize(("workflow", "job_fields"), [
    (TodoWorkflow, {"type": AgentType.TODO}),
    (FollowupWorkflow, {"type": AgentType.FOLLOWUP, "context": "check again"}),
    (BlankWorkflow, {"type": AgentType.BLANK}),
    (CronWorkflow, {"type": AgentType.CRON}),
    (SubagentWorkflow, {"type": AgentType.BLANK, "context": "the delegated task"}),
    # ConversationWorkflow reads its history from the DB: tests/integration/test_resume_semantics.py.
])
def test_every_framework_workflow_passes_the_context(workflow, job_fields):
    job = Job.stub(source="test", reference_id="R-1", **job_fields)
    runner = _Runner(True)
    workflow(runner, logging.getLogger("t")).execute_job(TestChannel(), job, _context("s-1"))
    assert runner.requests[0].session_id == "s-1"
