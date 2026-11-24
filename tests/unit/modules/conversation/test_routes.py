"""The run's tool risk assessment is admin-only on read (Task 7, PRD tool labels §4)."""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch

import pytest

from agento.framework.access.accounts import User
from agento.modules.conversation.src import routes

RISK = {"v": 1, "trifecta": True, "tool_coverage": "complete", "coverage": "complete",
        "direct": "recorded", "categories": {}, "uncovered": [], "notes": []}
S = "agento.modules.conversation.src.routes.service"


def _run_row(tool_risk) -> dict:
    t = datetime(2026, 11, 24, 8, 0)
    return {"execution_id": "e-1", "job_id": 1, "attempt": 1, "status": "succeeded",
            "started_at": t, "finished_at": t, "type": "conversation", "harness": "h",
            "provider": "p", "credential": None, "model": None, "input_tokens": None,
            "output_tokens": None, "parent_job_id": None, "depth": 0, "resume_job_id": None,
            "agent_view": "av", "tool_risk": tool_risk}


class _Req:
    def __init__(self, role: str):
        self.conn = object()
        self.params = {"id": "5"}
        self.query = {}
        self.session = type("S", (), {"user": User(id=1, username="u", role=role, is_active=True)})()


def _show(role: str, *, details: bool, stored) -> list[dict]:
    t = datetime(2026, 11, 24, 8, 0)
    conversation = {"id": 5, "agent_view_id": 3, "title": "t", "status": "active", "created_at": t,
                    "updated_at": t, "channel": "panel", "external_ref": None,
                    "last_activity_at": t, "pinned_at": None, "thread_id": None}
    with patch(f"{S}.load_visible", return_value=conversation), \
            patch(f"{S}.is_live", return_value=False), \
            patch(f"{S}.can_see_run_details", return_value=details), \
            patch(f"{S}.open_turn", return_value=None), \
            patch(f"{S}.list_runs", return_value=[_run_row(stored)]):
        return routes.show(_Req(role)).body["runs"]  # type: ignore[union-attr]


@pytest.mark.parametrize("stored", [json.dumps(RISK), RISK])
def test_tool_risk_admin_only(stored):
    assert _show("admin", details=True, stored=stored)[0]["tool_risk"] == RISK
    # A non-admin holding `conversation.run_details` sees every run detail but the assessment.
    granted = _show("user", details=True, stored=stored)[0]
    assert "harness" in granted and "tool_risk" not in granted
    assert "tool_risk" not in _show("user", details=False, stored=stored)[0]


def test_an_old_run_has_null_tool_risk():
    assert _show("admin", details=True, stored=None)[0]["tool_risk"] is None


def test_tool_risk_not_in_messages_or_events():
    """The message shape and the run.started payload never carry the assessment."""
    import inspect

    from agento.modules.conversation.src import service

    message = routes._message_json({
        "id": 1, "role": "user", "content": "x", "uploads": None, "client_message_id": None,
        "job_id": 1, "job_state": None, "created_at": datetime(2026, 11, 24), "tool_risk": RISK})
    assert "tool_risk" not in message
    assert "tool_risk" not in inspect.getsource(service.link_execution)
    for handler in (routes.messages, routes.events, routes.timeline):
        assert "tool_risk" not in inspect.getsource(handler)
