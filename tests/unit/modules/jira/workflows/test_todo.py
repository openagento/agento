from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agento.framework.channels.base import PromptFragments, WorkItem
from agento.framework.harness import RunResult
from agento.framework.job_models import AgentType, Job
from agento.framework.workflows.base import JobContext
from agento.framework.workflows.todo import TodoWorkflow
from agento.modules.jira.src.channel import JiraChannel


def _mock_channel(name="test", **overrides):
    defaults = dict(
        read_context="Read context.",
        respond="Post result.",
        transition_start=None,
        transition_done=None,
        assign_back=None,
        ask_and_handback=None,
        extra=None,
    )
    defaults.update(overrides)

    ch = MagicMock()
    ch.name = name
    ch.get_prompt_fragments.return_value = PromptFragments(**defaults)
    return ch


class TestTodoWorkflow:
    def setup_method(self):
        self.runner = MagicMock()
        self.logger = MagicMock()
        self.workflow = TodoWorkflow(self.runner, self.logger)

    def test_full_jira_prompt(self):
        jira = JiraChannel()
        prompt = self.workflow.build_prompt(jira, "AI-456")

        assert "jira_get_issue" in prompt
        assert "AI-456" in prompt
        assert "In Progress" in prompt
        assert "jira_transition_issue" in prompt
        assert "jira_add_comment" in prompt
        assert "jira_assign_issue" in prompt
        assert "Review" in prompt
        assert "reporter" in prompt
        # Should have 6 steps
        assert "KROK 6" in prompt

    def test_minimal_channel_no_transitions(self):
        channel = _mock_channel(
            read_context="Read the email thread.",
            respond="Reply to the email.",
        )
        prompt = self.workflow.build_prompt(channel, "msg-123")

        assert "Read the email thread" in prompt
        assert "Reply to the email" in prompt
        # No transition steps
        assert "In Progress" not in prompt
        assert "Review" not in prompt
        assert "reporter" not in prompt
        # Should have 3 steps (read, execute, respond) - no transition, no evaluate, no finish
        assert "KROK 3" in prompt
        assert "KROK 4" not in prompt

    def test_channel_with_transitions_but_no_evaluate(self):
        channel = _mock_channel(
            read_context="Read message.",
            respond="Post reply.",
            transition_start="Set status to active.",
            transition_done="Set status to done.",
            assign_back="Assign to sender.",
        )
        prompt = self.workflow.build_prompt(channel, "ref-1")

        assert "KROK 2" in prompt  # transition start
        assert "Set status to active" in prompt
        assert "Oceń" not in prompt  # no evaluate step
        assert "Set status to done" in prompt
        assert "Assign to sender" in prompt

    def test_step_numbering_adapts(self):
        # Full features: 6 steps
        jira = JiraChannel()
        prompt = self.workflow.build_prompt(jira, "AI-1")
        assert "KROK 6" in prompt

        # Minimal: 3 steps
        channel = _mock_channel()
        prompt = self.workflow.build_prompt(channel, "ref-1")
        assert "KROK 3" in prompt
        assert "KROK 4" not in prompt


def _make_job(*, reference_id=None, id=1):
    return Job.stub(type=AgentType.TODO, source="jira", reference_id=reference_id)


def _make_context(update_ref=None):
    return JobContext(
        config=MagicMock(),
        logger=MagicMock(),
        update_reference_id=update_ref or MagicMock(),
    )


class TestTodoExecuteJob:
    def setup_method(self):
        self.runner = MagicMock()
        self.runner.execute.return_value = RunResult(
            raw_output="OK", input_tokens=100, output_tokens=50, session_id="success"
        )
        self.logger = MagicMock()
        self.workflow = TodoWorkflow(self.runner, self.logger)

    def test_with_reference_id(self):
        channel = _mock_channel()
        job = _make_job(reference_id="AI-42")
        context = _make_context()

        result = self.workflow.execute_job(channel, job, context)

        assert isinstance(result, RunResult)
        self.runner.execute.assert_called_once()
        context.update_reference_id.assert_not_called()

    def test_discovery_found(self):
        channel = MagicMock()
        channel.name = "jira"
        channel.discover_work.return_value = [
            WorkItem(reference_id="AI-50", title="Do something",
                     priority=2, reason="Assigned", source_tag="todo")
        ]
        channel.get_prompt_fragments.return_value = PromptFragments(
            read_context="Read.", respond="Post."
        )
        update_ref = MagicMock()
        job = _make_job()
        context = _make_context(update_ref=update_ref)

        result = self.workflow.execute_job(channel, job, context)

        assert isinstance(result, RunResult)
        channel.discover_work.assert_called_once()
        update_ref.assert_called_once_with(job.id, "AI-50")

    def test_discovery_empty(self):
        channel = MagicMock()
        channel.name = "jira"
        channel.discover_work.return_value = []
        job = _make_job()
        context = _make_context()

        result = self.workflow.execute_job(channel, job, context)

        # No agent ran, so there is no session to resume from. The old code stashed a
        # "no_work" MARKER in this field, which the consumer then persisted as
        # job.session_id — a resume of attempt 2 would have targeted a session id that
        # never existed.
        assert result.session_id is None
        assert result.raw_output == "No TODO tasks found"
        assert result.input_tokens is None
        self.runner.execute.assert_not_called()

    def test_non_discoverable_channel_raises(self):
        channel = MagicMock(spec=["name", "get_prompt_fragments", "get_followup_fragments"])
        channel.name = "email"
        job = _make_job()
        context = _make_context()

        with pytest.raises(ValueError, match="does not support work discovery"):
            self.workflow.execute_job(channel, job, context)
