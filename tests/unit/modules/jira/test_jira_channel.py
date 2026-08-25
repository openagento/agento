from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from agento.framework.channels.base import Channel, DiscoverableChannel
from agento.framework.job_models import AgentType
from agento.modules.jira.src.channel import JiraChannel


class TestJiraChannelProtocol:
    def test_satisfies_channel_protocol(self):
        assert isinstance(JiraChannel(), Channel)

    def test_satisfies_discoverable_protocol(self):
        assert isinstance(JiraChannel(), DiscoverableChannel)

    def test_name(self):
        assert JiraChannel().name == "jira"


class TestPromptFragments:
    def setup_method(self):
        self.jira = JiraChannel()
        self.f = self.jira.get_prompt_fragments("AI-123")

    def test_read_context_contains_tool_and_key(self):
        assert "jira_get_issue" in self.f.read_context
        assert "AI-123" in self.f.read_context

    def test_respond_contains_tool(self):
        assert "jira_add_comment" in self.f.respond

    def test_transition_start(self):
        assert "jira_transition_issue" in self.f.transition_start
        assert "In Progress" in self.f.transition_start

    def test_transition_done(self):
        assert "Review" in self.f.transition_done

    def test_assign_back(self):
        assert "jira_assign_issue" in self.f.assign_back
        assert "reporter" in self.f.assign_back

    def test_ask_and_handback(self):
        assert "jira_add_comment" in self.f.ask_and_handback
        assert "ZAKOŃCZ" in self.f.ask_and_handback

    def test_extra_is_none(self):
        assert self.f.extra is None


class TestFollowupFragments:
    def setup_method(self):
        self.jira = JiraChannel()
        self.f = self.jira.get_followup_fragments("AI-456", "Sprawdź reindeks")

    def test_read_context(self):
        assert "jira_get_issue" in self.f.read_context
        assert "AI-456" in self.f.read_context

    def test_respond(self):
        assert "jira_add_comment" in self.f.respond

    def test_no_transition_start(self):
        assert self.f.transition_start is None

    def test_transition_done(self):
        assert "Review" in self.f.transition_done

    def test_extra_contains_instructions(self):
        assert "Sprawdź reindeks" in self.f.extra
        assert "KONTEKST" in self.f.extra

    def test_no_ask_and_handback(self):
        assert self.f.ask_and_handback is None


class TestDiscoverWork:
    @patch("agento.modules.jira.src.channel.ToolboxClient")
    @patch("agento.modules.jira.src.channel.TaskListBuilder")
    def test_discover_wraps_task_list_builder(self, MockBuilder, MockToolbox, sample_config):
        mock_task = MagicMock()
        mock_task.issue.key = "AI-50"
        mock_task.issue.summary = "Do something"
        mock_task.priority.value = 2
        mock_task.reason = "Assigned in TODO"
        mock_task.source.value = "todo_assigned"
        mock_task.issue.updated = "2026-02-20T08:00:00"
        MockBuilder.return_value.get_todo_tasks.return_value = [mock_task]

        jira = JiraChannel()
        import logging
        items = jira.discover_work(
            sample_config, logging.getLogger("test"), capability_token="cap",
        )

        assert len(items) == 1
        assert items[0].reference_id == "AI-50"
        assert items[0].title == "Do something"
        assert items[0].priority == 2

    @patch("agento.modules.jira.src.channel.ToolboxClient")
    @patch("agento.modules.jira.src.channel.TaskListBuilder")
    def test_discover_empty(self, MockBuilder, MockToolbox, sample_config):
        MockBuilder.return_value.get_todo_tasks.return_value = []

        jira = JiraChannel()
        import logging
        items = jira.discover_work(
            sample_config, logging.getLogger("test"), capability_token="cap",
        )
        assert items == []


class TestIdempotencyKey:
    def setup_method(self):
        self.jira = JiraChannel()

    @patch("agento.modules.jira.src.channel.datetime")
    def test_cron_key(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 2, 20, 8, 0)
        key = self.jira.build_idempotency_key(AgentType.CRON, "AI-123")
        assert key == "jira:cron:AI-123:20260220_0800"

    @patch("agento.modules.jira.src.channel.datetime")
    def test_todo_key(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 2, 20, 8, 0)
        key = self.jira.build_idempotency_key(AgentType.TODO, "AI-456")
        assert key == "jira:todo:AI-456:20260220_0800"

    @patch("agento.modules.jira.src.channel.datetime")
    def test_todo_key_with_updated(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 2, 20, 8, 0)
        key = self.jira.build_idempotency_key(
            AgentType.TODO, "AI-456", updated="2026-02-20T16:45:00.000+0000"
        )
        assert key == "jira:todo:AI-456:u20260220_1645"

    @patch("agento.modules.jira.src.channel.datetime")
    def test_dispatch_key(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 2, 20, 8, 0)
        key = self.jira.build_idempotency_key(AgentType.TODO, None)
        assert key == "jira:todo:dispatch:20260220_0800"
