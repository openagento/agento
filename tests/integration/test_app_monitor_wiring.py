"""End-to-end wiring test for the ``app_monitor`` module.

Relies on the session-level ``_bootstrap_registries`` fixture in
``tests/integration/conftest.py`` having loaded the module manifests, observer
registrations, and the TranscriptReader registry via the real ``import_class``
machinery. Dispatching a real event here exercises:

  events.json → observer class import → JobFinalizeEvent → find_harness →
  ClaudeTranscriptReader → JSONL parse → toolbox-call count → _save_mcp_telemetry

— as a single chain. Broken wiring (events.json, di.json, registry, protocol
implementation) would fail here even when isolated unit tests pass.

The observer is telemetry-only: it records ``toolbox_mcp_calls`` /
``toolbox_mcp_connected`` and never sets a verdict. We stub ``_save_mcp_telemetry``
to capture the computed signals without needing a DB, and confirm ``verdict``
stays ``None`` on every path.

NOTE: we never call ``clear_event_manager`` here. The integration session
shares one EventManager across all tests; clearing it would de-register every
other module's observers and silently break unrelated downstream tests.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agento.framework.event_manager import get_event_manager
from agento.framework.events import JobFinalizeEvent
from agento.modules.app_monitor.src import observers as obs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "transcripts"
CODEX_FIXTURES = FIXTURES / "codex"

CODEX_GOOD_ID = "11111111-1111-1111-1111-111111111111"
CODEX_BAD_ID = "22222222-2222-2222-2222-222222222222"


@dataclass
class _Job:
    id: int = 1
    reference_id: str = "AI-70"
    source: str = "jira"
    attempt: int = 1
    max_attempts: int = 3
    session_id: str | None = None


@pytest.fixture
def workspace_root(tmp_path: Path, monkeypatch) -> Path:
    """Build a production-shape build tree with fixture JSONLs and redirect the
    real ``ClaudeTranscriptReader`` at it via ``BUILD_DIR``.
    """
    projects = (
        tmp_path / "build" / "acme" / "developer" / "build-001"
        / ".claude" / "projects" / "-workspace-x"
    )
    projects.mkdir(parents=True)
    for src in FIXTURES.glob("*.jsonl"):
        shutil.copy(src, projects / f"{src.stem}.jsonl")
    from agento.modules.claude.src import transcript_reader as claude_tr
    monkeypatch.setattr(claude_tr, "BUILD_DIR", str(tmp_path / "build"))
    # Flag off + no SMTP so no alert path runs during the wiring check.
    monkeypatch.setattr(obs, "_config", lambda: {})
    return tmp_path


def test_telemetry_observer_records_good_transcript(workspace_root, monkeypatch):
    saver = MagicMock()
    monkeypatch.setattr(obs, "_save_mcp_telemetry", saver)
    event = JobFinalizeEvent(
        job=_Job(session_id="good_with_mcp"), job_result=None, harness="claude",
    )
    get_event_manager().dispatch("job_finalize_before", event)
    # Telemetry only — never vetoes.
    assert event.verdict is None
    # good_with_mcp has exactly one mcp__toolbox__* call; job_result=None → connected unknown.
    saver.assert_called_once_with(1, 1, None)


def test_telemetry_observer_records_zero_calls_bad_transcript(workspace_root, monkeypatch):
    saver = MagicMock()
    monkeypatch.setattr(obs, "_save_mcp_telemetry", saver)
    event = JobFinalizeEvent(
        job=_Job(session_id="bad_no_mcp"), job_result=None, harness="claude",
    )
    get_event_manager().dispatch("job_finalize_before", event)
    assert event.verdict is None  # zero calls is recorded, NOT vetoed
    saver.assert_called_once_with(1, 0, None)


def test_telemetry_observer_null_calls_when_no_reader_for_harness(workspace_root, monkeypatch):
    saver = MagicMock()
    monkeypatch.setattr(obs, "_save_mcp_telemetry", saver)
    event = JobFinalizeEvent(
        job=_Job(session_id="any"), job_result=None, harness="unregistered",
    )
    get_event_manager().dispatch("job_finalize_before", event)
    assert event.verdict is None  # no reader → calls unknown → NULL, no veto
    saver.assert_called_once_with(1, None, None)


@pytest.fixture
def codex_workspace_root(tmp_path: Path, monkeypatch) -> Path:
    """Lay codex fixture JSONLs under the production BUILD_DIR shape and
    redirect the real ``CodexTranscriptReader`` at it via ``BUILD_DIR``.
    """
    sessions = (
        tmp_path / "build" / "acme" / "developer" / "build-001"
        / ".codex" / "sessions" / "2026" / "05" / "14"
    )
    sessions.mkdir(parents=True)
    shutil.copy(
        CODEX_FIXTURES / "codex_good_with_mcp.jsonl",
        sessions / f"rollout-2026-05-14T05-05-33-{CODEX_GOOD_ID}.jsonl",
    )
    shutil.copy(
        CODEX_FIXTURES / "codex_bad_no_mcp.jsonl",
        sessions / f"rollout-2026-05-14T05-10-00-{CODEX_BAD_ID}.jsonl",
    )
    from agento.modules.codex.src import transcript_reader as codex_tr
    monkeypatch.setattr(codex_tr, "BUILD_DIR", str(tmp_path / "build"))
    monkeypatch.setattr(obs, "_config", lambda: {})
    return tmp_path


def test_codex_reader_registered_via_bootstrap():
    """Bootstrap loads codex/di.json → CodexTranscriptReader hangs off the harness."""
    from agento.framework.harness import find_harness
    from agento.modules.codex.src.transcript_reader import CodexTranscriptReader

    registered = find_harness("codex")
    assert registered is not None
    assert isinstance(registered.adapter.transcript_reader, CodexTranscriptReader)


def test_telemetry_observer_records_good_codex_transcript(codex_workspace_root, monkeypatch):
    saver = MagicMock()
    monkeypatch.setattr(obs, "_save_mcp_telemetry", saver)
    event = JobFinalizeEvent(
        job=_Job(session_id=CODEX_GOOD_ID), job_result=None, harness="codex",
    )
    get_event_manager().dispatch("job_finalize_before", event)
    assert event.verdict is None
    # codex_good_with_mcp has three mcp__toolbox__* calls; codex emits no init → connected NULL.
    saver.assert_called_once_with(1, 3, None)


def test_telemetry_observer_records_zero_calls_bad_codex_transcript(codex_workspace_root, monkeypatch):
    saver = MagicMock()
    monkeypatch.setattr(obs, "_save_mcp_telemetry", saver)
    event = JobFinalizeEvent(
        job=_Job(session_id=CODEX_BAD_ID), job_result=None, harness="codex",
    )
    get_event_manager().dispatch("job_finalize_before", event)
    assert event.verdict is None
    saver.assert_called_once_with(1, 0, None)


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
