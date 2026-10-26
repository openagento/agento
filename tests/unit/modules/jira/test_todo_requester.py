"""Tests for _todo_requester - status-change actor with reporter fallback."""
from __future__ import annotations

from unittest.mock import MagicMock

from agento.framework.job_models import RequesterTrust
from agento.modules.jira.src.commands.publish import _todo_requester
from agento.modules.jira.src.models import JiraIssue


def _issue(**overrides) -> JiraIssue:
    defaults = dict(
        key="AI-1",
        summary="Test",
        status="In Progress",
        reporter="Reporter Name",
        reporter_account_id="rep-1",
        reporter_email="rep@example.com",
    )
    defaults.update(overrides)
    return JiraIssue(**defaults)


def _builder(status_change_return):
    builder = MagicMock()
    builder.config.jira_assignee_account_id = "agent-1"
    builder.get_status_change.return_value = status_change_return
    return builder


def test_status_change_actor():
    change = {
        "id": "55",
        "created": "2026-03-01T10:00:00.000+0100",
        "author": {"accountId": "mover-1", "emailAddress": "Mover@Example.com", "displayName": "Mover"},
    }
    requester = _todo_requester(_builder((change, True)), _issue())

    assert requester is not None
    assert requester.key == "jira:mover-1"
    assert requester.email == "mover@example.com"  # normalized
    assert requester.trust is RequesterTrust.ACCOUNT
    assert requester.meta["basis"] == "status_change"
    assert requester.meta["changelog_id"] == "55"
    assert requester.meta["changed_at"] == "2026-03-01T10:00:00.000+0100"
    assert requester.meta["status"] == "In Progress"


def test_reporter_fallback_no_status_transition():
    requester = _todo_requester(_builder((None, True)), _issue())

    assert requester is not None
    assert requester.key == "jira:rep-1"
    assert requester.email == "rep@example.com"
    assert requester.meta["basis"] == "reporter"
    assert requester.meta["fallback_reason"] == "no_status_transition"


def test_reporter_fallback_changelog_unavailable():
    requester = _todo_requester(_builder((None, False)), _issue())

    assert requester is not None
    assert requester.meta["basis"] == "reporter"
    assert requester.meta["fallback_reason"] == "changelog_unavailable"


def test_reporter_fallback_status_change_actor_unavailable():
    # a transition WAS found, but its author has no accountId -> unattributable actor
    change = {"id": "9", "created": "x", "author": {"displayName": "Anon"}}
    requester = _todo_requester(_builder((change, True)), _issue())

    assert requester is not None
    assert requester.meta["basis"] == "reporter"
    assert requester.meta["fallback_reason"] == "status_change_actor_unavailable"


def test_reporter_fallback_when_agent_moved_the_status():
    # the agent moves the issue back to To Do when it hands it back with a question
    change = {"id": "12", "created": "x", "author": {"accountId": "agent-1", "displayName": "Agento"}}
    requester = _todo_requester(_builder((change, True)), _issue(status="To Do"))

    assert requester is not None
    assert requester.key == "jira:rep-1"
    assert requester.meta["basis"] == "reporter"
    assert requester.meta["fallback_reason"] == "status_change_by_agent"


def test_returns_none_when_no_actor_and_no_reporter():
    issue = _issue(reporter_account_id=None)
    requester = _todo_requester(_builder((None, True)), issue)
    assert requester is None
