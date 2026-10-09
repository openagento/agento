"""End-to-end wiring test for the ``app_monitor`` module.

Relies on the session-level ``_bootstrap_registries`` fixture in
``tests/integration/conftest.py`` having loaded the module manifests and observer
registrations via the real ``import_class`` machinery. Dispatching a real event
here exercises:

  events.json → observer class import → JobFinalizeEvent → toolbox-call count →
  _save_mcp_telemetry

— as a single chain. Broken wiring (events.json, observer class) would fail here
even when isolated unit tests pass.

The observer is telemetry-only: it records ``toolbox_mcp_calls`` /
``toolbox_mcp_connected`` and never sets a verdict. We stub the DB (connection,
count, save) to capture the computed signals, and confirm ``verdict`` stays
``None`` on every path. The real count query runs in ``test_app_monitor_e2e.py``.

NOTE: we never call ``clear_event_manager`` here. The integration session
shares one EventManager across all tests; clearing it would de-register every
other module's observers and silently break unrelated downstream tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from agento.framework.event_manager import get_event_manager
from agento.framework.events import JobFinalizeEvent
from agento.modules.app_monitor.src import observers as obs


@dataclass
class _Job:
    id: int = 1
    reference_id: str = "AI-70"
    source: str = "jira"
    attempt: int = 1
    max_attempts: int = 3
    session_id: str | None = None


@pytest.fixture
def saver(monkeypatch) -> MagicMock:
    """No DB: 4 audited calls for job 1, unknown for any other job."""
    monkeypatch.setattr(obs, "get_connection", lambda _cfg: MagicMock())
    monkeypatch.setattr(
        obs, "_count_toolbox_calls", lambda _conn, job: 4 if job.id == 1 else None,
    )
    saver = MagicMock()
    monkeypatch.setattr(obs, "_save_mcp_telemetry", lambda _conn, *a: saver(*a))
    # Flag off + no SMTP so no alert path runs during the wiring check.
    monkeypatch.setattr(obs, "_config", lambda: {})
    return saver


def test_telemetry_observer_records_the_count(saver):
    event = JobFinalizeEvent(job=_Job(session_id="s1"), job_result=None, harness="claude")
    get_event_manager().dispatch("job_finalize_before", event)
    # Telemetry only — never vetoes. job_result=None → connected unknown.
    assert event.verdict is None
    saver.assert_called_once_with(1, 4, None)


def test_telemetry_observer_records_unknown_count(saver):
    event = JobFinalizeEvent(job=_Job(id=2, session_id="s2"), job_result=None, harness="codex")
    get_event_manager().dispatch("job_finalize_before", event)
    assert event.verdict is None  # count unknown → NULL, no veto
    saver.assert_called_once_with(2, None, None)


def test_alert_observer_sends_email_when_configured(monkeypatch):
    from agento.modules.app_monitor.src.constants import (
        CFG_ALERT_EMAIL_TO,
        CFG_ALERT_SMTP_FROM,
        CFG_ALERT_SMTP_HOST,
        CFG_ALERT_SMTP_PORT,
        CFG_ALERT_SMTP_TLS,
    )
    monkeypatch.setattr(obs, "_config", lambda: {
        CFG_ALERT_EMAIL_TO: "ops@example.com",
        CFG_ALERT_SMTP_HOST: "smtp.example.com",
        CFG_ALERT_SMTP_PORT: 587,
        CFG_ALERT_SMTP_FROM: "agento@example.com",
        CFG_ALERT_SMTP_TLS: False,
    })

    sender = MagicMock()
    monkeypatch.setattr(obs, "send_alert", sender)

    @dataclass
    class _DeadEvent:
        job: _Job
        error: Exception
        elapsed_ms: int = 0

    get_event_manager().dispatch(
        "job_dead_after",
        _DeadEvent(job=_Job(id=99), error=RuntimeError("ghost-success"), elapsed_ms=10),
    )

    sender.assert_called_once()
