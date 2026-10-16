"""Safety net: assert new workflow+channel prompts contain same key instructions
as old hardcoded templates."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agento.framework.workflows.followup import FollowupWorkflow
from agento.framework.workflows.todo import TodoWorkflow
from agento.modules.jira.src.channel import JiraChannel
from agento.modules.jira_periodic_tasks.src.workflows.cron import CronWorkflow

JIRA = JiraChannel()


@pytest.mark.parametrize("needle", [
    "jira_get_issue",
    "jira_add_comment",
    "AI-123",
    "Nie zmieniaj statusu",
    "cykliczne",
    "poprzednich uruchomień",
])
def test_cron_prompt_contains(needle):
    assert needle in CronWorkflow(MagicMock(), MagicMock()).build_prompt(JIRA, "AI-123")


@pytest.mark.parametrize("needle", [
    "jira_get_issue",
    "jira_transition_issue",
    "In Progress",
    "Review",
    "jira_add_comment",
    "jira_assign_issue",
    "reporter",
    "AI-456",
    "KROK 6",
    "ZAKOŃCZ",
    "Zaplanuj",
])
def test_todo_prompt_contains(needle):
    assert needle in TodoWorkflow(MagicMock(), MagicMock()).build_prompt(JIRA, "AI-456")


@pytest.mark.parametrize("needle", [
    "jira_get_issue",
    "jira_add_comment",
    "schedule_followup",
    "Sprawdź reindeks",
    "KONTEKST",
    "Review",
    "reporter",
    "follow-up",
])
def test_followup_prompt_contains(needle):
    prompt = FollowupWorkflow(MagicMock(), MagicMock()).build_prompt(
        JIRA, "AI-789", instructions="Sprawdź reindeks"
    )
    assert needle in prompt
