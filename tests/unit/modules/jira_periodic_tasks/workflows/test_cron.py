from __future__ import annotations

from unittest.mock import MagicMock

from agento.framework.channels.base import PromptFragments
from agento.modules.jira.src.channel import JiraChannel
from agento.modules.jira_periodic_tasks.src.workflows.cron import CronWorkflow


def _mock_channel(name="test", **overrides):
    defaults = dict(
        read_context="Read the context for {ref}",
        respond="Post the result.",
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


class TestCronWorkflow:
    def setup_method(self):
        self.runner = MagicMock()
        self.logger = MagicMock()
        self.workflow = CronWorkflow(self.runner, self.logger)

    def test_build_prompt_contains_key_parts(self):
        channel = _mock_channel(
            read_context="Wczytaj zadanie (jira_get_issue) AI-1.",
            respond="Wynik dodaj jako komentarz (jira_add_comment).",
        )
        prompt = self.workflow.build_prompt(channel, "AI-1")

        assert "jira_get_issue" in prompt
        assert "jira_add_comment" in prompt
        assert "AI-1" in prompt
        assert "Nie zmieniaj statusu" in prompt
        assert "cykliczne" in prompt.lower()

    def test_build_prompt_with_jira_channel(self):
        jira = JiraChannel()
        prompt = self.workflow.build_prompt(jira, "AI-123")

        assert "jira_get_issue" in prompt
        assert "jira_add_comment" in prompt
        assert "AI-123" in prompt
        assert "Nie zmieniaj statusu ani assignee" in prompt

    def test_execute_calls_runner(self):
        result = MagicMock()
        result.session_id = "success"
        result.stats_line = "turns=3"
        self.runner.execute.return_value = result

        channel = _mock_channel()
        ret = self.workflow.execute(channel, "AI-1")

        self.runner.execute.assert_called_once()
        assert ret is result

    def test_execute_stamps_prompt_on_result(self):
        from agento.framework.harness import RunResult
        result = RunResult(raw_output="ok", session_id="success")
        self.runner.execute.return_value = result

        channel = _mock_channel(
            read_context="Wczytaj zadanie AI-1.",
            respond="Dodaj komentarz.",
        )
        ret = self.workflow.execute(channel, "AI-1")

        assert ret.prompt is not None
        assert "AI-1" in ret.prompt
