from __future__ import annotations

from unittest.mock import MagicMock

from agento.framework.channels.base import PromptFragments
from agento.framework.workflows.followup import FollowupWorkflow
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
    ch.get_followup_fragments.return_value = PromptFragments(**defaults)
    return ch


class TestFollowupWorkflow:
    def setup_method(self):
        self.runner = MagicMock()
        self.logger = MagicMock()
        self.workflow = FollowupWorkflow(self.runner, self.logger)

    def test_jira_followup_prompt(self):
        jira = JiraChannel()
        prompt = self.workflow.build_prompt(
            jira, "AI-789", instructions="Sprawdź czy reindeks się zakończył"
        )

        assert "follow-up" in prompt.lower()
        assert "AI-789" in prompt
        assert "jira_get_issue" in prompt
        assert "jira_add_comment" in prompt
        assert "KONTEKST" in prompt
        assert "Sprawdź czy reindeks się zakończył" in prompt
        assert "schedule_followup" in prompt
        assert "Review" in prompt
        assert "reporter" in prompt

    def test_minimal_channel_followup(self):
        channel = _mock_channel(
            read_context="Read the thread.",
            respond="Reply to the thread.",
        )
        prompt = self.workflow.build_prompt(
            channel, "thread-abc", instructions="Check if resolved"
        )

        assert "Read the thread" in prompt
        assert "Reply to the thread" in prompt
        assert "schedule_followup" in prompt
        # No finish block (no transition_done or assign_back)
        assert "Review" not in prompt
        assert "reporter" not in prompt

    def test_followup_with_finish_block(self):
        channel = _mock_channel(
            read_context="Read.",
            respond="Reply.",
            transition_done="Close the ticket.",
            assign_back="Reassign to owner.",
        )
        prompt = self.workflow.build_prompt(
            channel, "ref-1", instructions="Check status"
        )

        assert "Close the ticket" in prompt
        assert "Reassign to owner" in prompt
        assert "5. Jeśli zadanie zakończone:" in prompt

    def test_execute_passes_instructions(self):
        result = MagicMock()
        result.session_id = "success"
        result.stats_line = "turns=2"
        self.runner.execute.return_value = result

        jira = JiraChannel()
        ret = self.workflow.execute(jira, "AI-1", instructions="Check reindex")

        self.runner.execute.assert_called_once()
        prompt = self.runner.execute.call_args.args[0].prompt
        assert "Check reindex" in prompt
        assert ret is result
