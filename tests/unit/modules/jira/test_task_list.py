from __future__ import annotations

import logging
from unittest.mock import MagicMock

import httpx
import respx

from agento.modules.jira.src.models import TaskPriority, TaskSource
from agento.modules.jira.src.task_list import TaskListBuilder
from agento.modules.jira.src.toolbox_client import ToolboxClient


def _make_builder(toolbox: ToolboxClient, sample_config) -> TaskListBuilder:
    return TaskListBuilder(
        toolbox=toolbox,
        config=sample_config,
        ai_user="agenty@example.com",
        logger=logging.getLogger("test-task-list"),
    )


def _status_entry(entry_id: str, to_status: str, *, account_id: str | None = "u-1") -> dict:
    author: dict = {"displayName": "Mover"}
    if account_id is not None:
        author["accountId"] = account_id
    return {
        "id": entry_id,
        "created": "2026-03-01T10:00:00.000+0100",
        "author": author,
        "items": [{"field": "status", "fieldtype": "jira", "toString": to_status}],
    }


def _changelog_builder(toolbox: MagicMock) -> TaskListBuilder:
    return TaskListBuilder(
        toolbox=toolbox,
        config=MagicMock(),
        ai_user="agenty@example.com",
        logger=logging.getLogger("test-changelog"),
        agent_view_id=7,
    )


@respx.mock
def test_get_todo_tasks(sample_config, jira_todo):
    respx.post("http://toolbox:3001/api/jira/search").mock(
        return_value=httpx.Response(200, json=jira_todo)
    )
    client = ToolboxClient("http://toolbox:3001", capability_token="test-cap")
    builder = _make_builder(client, sample_config)

    tasks = builder.get_todo_tasks()

    assert len(tasks) == 2
    assert tasks[0].source == TaskSource.TODO_ASSIGNED
    assert tasks[0].priority == TaskPriority.HIGH
    assert tasks[0].issue.key == "AI-10"
    assert tasks[0].issue.summary == "Zaktualizuj dokumentację API"
    assert tasks[1].issue.key == "AI-11"


@respx.mock
def test_get_todo_tasks_empty(sample_config, jira_empty):
    respx.post("http://toolbox:3001/api/jira/search").mock(
        return_value=httpx.Response(200, json=jira_empty)
    )
    client = ToolboxClient("http://toolbox:3001", capability_token="test-cap")
    builder = _make_builder(client, sample_config)

    tasks = builder.get_todo_tasks()
    assert tasks == []


def test_parse_issue():
    raw = {
        "key": "AI-99",
        "fields": {
            "summary": "Test issue",
            "status": {"name": "In Progress"},
            "priority": {"name": "Medium"},
            "assignee": {"displayName": "Bot", "accountId": "bot-1"},
            "reporter": {"displayName": "Human", "accountId": "human-1"},
            "created": "2026-01-01T00:00:00.000+0100",
            "updated": "2026-02-01T00:00:00.000+0100",
        },
    }
    raw["fields"]["reporter"]["emailAddress"] = "human@example.com"
    issue = TaskListBuilder._parse_issue(raw)
    assert issue.key == "AI-99"
    assert issue.summary == "Test issue"
    assert issue.status == "In Progress"
    assert issue.assignee == "Bot"
    assert issue.assignee_account_id == "bot-1"
    assert issue.reporter == "Human"
    assert issue.reporter_account_id == "human-1"
    assert issue.reporter_email == "human@example.com"
    assert issue.priority == "Medium"


def test_parse_issue_reporter_email_absent():
    raw = {
        "key": "AI-1",
        "fields": {
            "summary": "No email",
            "reporter": {"displayName": "Human", "accountId": "human-1"},
        },
    }
    issue = TaskListBuilder._parse_issue(raw)
    assert issue.reporter_email is None


class TestGetStatusChange:
    def test_picks_latest_transition_into_current_status(self):
        toolbox = MagicMock()
        toolbox.jira_request.return_value = {
            "startAt": 0, "maxResults": 100, "total": 2, "isLast": True,
            "values": [_status_entry("100", "In Progress"), _status_entry("200", "In Progress")],
        }
        builder = _changelog_builder(toolbox)

        entry, complete = builder.get_status_change("AI-1", "In Progress")
        assert complete is True
        assert entry["id"] == "200"  # latest match wins

    def test_paginates_and_finds_match_on_last_page(self):
        toolbox = MagicMock()
        page1 = {
            "startAt": 0, "maxResults": 100, "total": 4, "isLast": False,
            "values": [_status_entry("1", "To Do"), _status_entry("2", "To Do")],
        }
        page2 = {
            "startAt": 2, "maxResults": 100, "total": 4, "isLast": True,
            "values": [_status_entry("3", "In Progress"), _status_entry("4", "Review")],
        }
        toolbox.jira_request.side_effect = [page1, page2]
        builder = _changelog_builder(toolbox)

        entry, complete = builder.get_status_change("AI-1", "In Progress")
        assert complete is True
        assert entry["id"] == "3"
        # second call must follow the response-derived offset (startAt 0 + len(page1.values)=2)
        assert toolbox.jira_request.call_count == 2
        second_path = toolbox.jira_request.call_args_list[1][0][1]
        assert "startAt=2" in second_path

    def test_no_matching_transition_full_scan(self):
        toolbox = MagicMock()
        toolbox.jira_request.return_value = {
            "startAt": 0, "maxResults": 100, "total": 1, "isLast": True,
            "values": [_status_entry("1", "Done")],
        }
        builder = _changelog_builder(toolbox)

        entry, complete = builder.get_status_change("AI-1", "In Progress")
        assert entry is None
        assert complete is True

    def test_fetch_error_returns_incomplete(self):
        toolbox = MagicMock()
        toolbox.jira_request.side_effect = RuntimeError("boom")
        builder = _changelog_builder(toolbox)

        entry, complete = builder.get_status_change("AI-1", "In Progress")
        assert entry is None
        assert complete is False

    def test_page_cap_returns_incomplete(self):
        toolbox = MagicMock()
        # never isLast -> hits the page cap
        toolbox.jira_request.return_value = {
            "startAt": 0, "maxResults": 100, "total": 9999, "isLast": False,
            "values": [_status_entry("x", "Done")],
        }
        builder = _changelog_builder(toolbox)

        entry, complete = builder.get_status_change("AI-1", "In Progress")
        assert entry is None
        assert complete is False
        assert toolbox.jira_request.call_count == TaskListBuilder._CHANGELOG_MAX_PAGES

    def test_issue_key_url_encoded_in_path(self):
        toolbox = MagicMock()
        toolbox.jira_request.return_value = {
            "startAt": 0, "maxResults": 100, "total": 0, "isLast": True, "values": [],
        }
        builder = _changelog_builder(toolbox)

        builder.get_status_change("AI 1/2", "In Progress")
        path = toolbox.jira_request.call_args_list[0][0][1]
        assert "AI%201%2F2" in path
        assert "AI 1/2" not in path


def test_parse_issue_null_fields():
    raw = {
        "key": "AI-100",
        "fields": {
            "summary": "Minimal",
            "status": None,
            "priority": None,
            "assignee": None,
            "reporter": None,
            "created": None,
            "updated": None,
        },
    }
    issue = TaskListBuilder._parse_issue(raw)
    assert issue.key == "AI-100"
    assert issue.status == ""
    assert issue.assignee is None
    assert issue.reporter is None
    assert issue.priority is None
