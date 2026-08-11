from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agento.framework.events import JobVerificationFailed, Verdict, VerifyReason
from agento.framework.harness import McpInitReport, McpServerStatus, ParseSummary, ToolUse
from agento.modules.app_monitor.src import observers as obs
from agento.modules.app_monitor.src.constants import (
    CFG_ALERT_EMAIL_TO,
    CFG_ALERT_SMTP_FROM,
    CFG_ALERT_SMTP_HOST,
    CFG_ALERT_SMTP_PASSWORD,
    CFG_ALERT_SMTP_PORT,
    CFG_ALERT_SMTP_TLS,
    CFG_ALERT_SMTP_USER,
    CFG_SEND_ALERT_ON_MCP_ISSUES,
)


@dataclass
class _Job:
    id: int = 7
    reference_id: str = "AI-70"
    source: str = "jira"
    attempt: int = 1
    max_attempts: int = 3
    session_id: str | None = None


@dataclass
class _JobResult:
    mcp_init: object | None = None


@dataclass
class _FinalizeEvent:
    job: _Job
    verdict: object | None = None
    elapsed_ms: int = 0
    job_result: object | None = None
    harness: str | None = "claude"


@dataclass
class _DeadEvent:
    job: _Job
    error: Exception
    elapsed_ms: int = 0


def _harness_with(reader):
    """A RegisteredHarness-shaped stub — the observer reads the transcript reader off
    the registered harness's adapter, not off ``find_harness`` directly."""
    return SimpleNamespace(adapter=SimpleNamespace(transcript_reader=reader))


class _FakeReader:
    """In-memory TranscriptReader stub keyed by session_id."""

    def __init__(self, summaries: dict[str, ParseSummary] | None = None):
        self.summaries = summaries or {}

    def parse(self, session_id: str) -> ParseSummary:
        if session_id not in self.summaries:
            raise FileNotFoundError(session_id)
        return self.summaries[session_id]

    def iter_tool_uses(self, session_id: str) -> tuple[ToolUse, ...]:
        return self.parse(session_id).tool_uses


def _summary(tool_use_names: list[str], *, total_json_lines: int | None = None,
             recognized_records: int | None = None) -> ParseSummary:
    tool_uses = tuple(ToolUse(name=n, tool_use_id=f"t{i}") for i, n in enumerate(tool_use_names))
    if recognized_records is None:
        recognized_records = max(len(tool_uses), 1)
    if total_json_lines is None:
        total_json_lines = recognized_records + 2
    return ParseSummary(
        total_json_lines=total_json_lines,
        recognized_records=recognized_records,
        tool_uses=tool_uses,
    )


def _mcp_init(*pairs: tuple[str, str]) -> McpInitReport:
    return McpInitReport(servers=tuple(McpServerStatus(n, s) for n, s in pairs))


def _toolbox(n: int) -> list[str]:
    """n distinct ``mcp__toolbox__*`` tool-use names."""
    return [f"mcp__toolbox__tool_{i}" for i in range(n)]


# Full SMTP config so _smtp_config() returns a usable object.
_SMTP = {
    CFG_ALERT_EMAIL_TO: "ops@example.com",
    CFG_ALERT_SMTP_HOST: "smtp.example.com",
    CFG_ALERT_SMTP_PORT: 587,
    CFG_ALERT_SMTP_USER: "u",
    CFG_ALERT_SMTP_PASSWORD: "p",
    CFG_ALERT_SMTP_FROM: "agento@example.com",
    CFG_ALERT_SMTP_TLS: True,
}


@pytest.fixture
def fake_reader(monkeypatch):
    reader = _FakeReader({
        "calls_3": _summary([*_toolbox(3), "Read"]),
        "calls_5": _summary(_toolbox(5)),
        "zero_calls": _summary(["Read", "Bash"]),
        "drifted": ParseSummary(total_json_lines=10, recognized_records=0, tool_uses=()),
    })
    monkeypatch.setattr(obs, "find_harness", lambda harness: _harness_with(reader))
    return reader


def _patch_config(monkeypatch, **kwargs):
    monkeypatch.setattr(obs, "_config", lambda: kwargs)


def _patch_saver(monkeypatch) -> MagicMock:
    saver = MagicMock()
    monkeypatch.setattr(obs, "_save_mcp_telemetry", saver)
    return saver


def _patch_sender(monkeypatch) -> MagicMock:
    sender = MagicMock()
    monkeypatch.setattr(obs, "send_alert", sender)
    return sender


class TestMcpHealthTelemetry:
    """Telemetry truth table — dual nullable signals, combined alert, no verdict."""

    def test_persists_both_signals_connected_with_calls(self, fake_reader, monkeypatch):
        _patch_config(monkeypatch)  # flag off (missing key)
        saver = _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=10, session_id="calls_3"),
            job_result=_JobResult(_mcp_init(("toolbox", "connected"))),
        )
        obs.McpHealthTelemetryObserver().execute(event)
        saver.assert_called_once_with(10, 3, True)
        sender.assert_not_called()
        assert event.verdict is None

    def test_persists_connected_zero_calls_no_alert(self, fake_reader, monkeypatch):
        _patch_config(monkeypatch, **{CFG_SEND_ALERT_ON_MCP_ISSUES: False, **_SMTP})
        saver = _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=11, session_id="zero_calls"),
            job_result=_JobResult(_mcp_init(("toolbox", "connected"))),
        )
        obs.McpHealthTelemetryObserver().execute(event)
        saver.assert_called_once_with(11, 0, True)
        sender.assert_not_called()
        assert event.verdict is None

    def test_persists_connected_zero_calls_alerts(self, fake_reader, monkeypatch):
        _patch_config(monkeypatch, **{CFG_SEND_ALERT_ON_MCP_ISSUES: True, **_SMTP})
        saver = _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=12, session_id="zero_calls"),
            job_result=_JobResult(_mcp_init(("toolbox", "connected"))),
        )
        obs.McpHealthTelemetryObserver().execute(event)
        saver.assert_called_once_with(12, 0, True)
        sender.assert_called_once()
        _, _, subject, _ = sender.call_args.args
        assert "0 toolbox calls" in subject
        assert event.verdict is None

    def test_persists_not_connected_with_calls_alerts(self, fake_reader, monkeypatch):
        _patch_config(monkeypatch, **{CFG_SEND_ALERT_ON_MCP_ISSUES: True, **_SMTP})
        saver = _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=13, session_id="calls_5"),
            job_result=_JobResult(_mcp_init(("toolbox", "failed"))),
        )
        obs.McpHealthTelemetryObserver().execute(event)
        saver.assert_called_once_with(13, 5, False)
        sender.assert_called_once()
        _, _, subject, _ = sender.call_args.args
        assert "toolbox not connected" in subject
        assert event.verdict is None

    def test_persists_not_connected_no_calls_alerts_once(self, fake_reader, monkeypatch):
        _patch_config(monkeypatch, **{CFG_SEND_ALERT_ON_MCP_ISSUES: True, **_SMTP})
        saver = _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=14, session_id="zero_calls"),
            job_result=_JobResult(_mcp_init(("toolbox", "failed"))),
        )
        obs.McpHealthTelemetryObserver().execute(event)
        saver.assert_called_once_with(14, 0, False)
        # Combined condition -> exactly ONE email naming both.
        sender.assert_called_once()
        _, _, subject, _ = sender.call_args.args
        assert "0 toolbox calls" in subject
        assert "toolbox not connected" in subject
        assert event.verdict is None

    def test_persists_no_init_data_with_calls(self, fake_reader, monkeypatch):
        _patch_config(monkeypatch, **{CFG_SEND_ALERT_ON_MCP_ISSUES: True, **_SMTP})
        saver = _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=15, session_id="calls_5"),
            job_result=_JobResult(mcp_init=None),
        )
        obs.McpHealthTelemetryObserver().execute(event)
        saver.assert_called_once_with(15, 5, None)
        sender.assert_not_called()  # NULL connected is "unknown", not "bad"
        assert event.verdict is None

    def test_null_calls_does_not_alert(self, monkeypatch):
        _patch_config(monkeypatch, **{CFG_SEND_ALERT_ON_MCP_ISSUES: True, **_SMTP})
        monkeypatch.setattr(obs, "find_harness", lambda harness: None)
        saver = _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=16, session_id="any"),
            harness="unknown",
            job_result=_JobResult(_mcp_init(("toolbox", "connected"))),
        )
        obs.McpHealthTelemetryObserver().execute(event)
        saver.assert_called_once_with(16, None, True)
        sender.assert_not_called()  # calls == 0 is False for None
        assert event.verdict is None

    def test_null_connected_does_not_alert(self, fake_reader, monkeypatch):
        _patch_config(monkeypatch, **{CFG_SEND_ALERT_ON_MCP_ISSUES: True, **_SMTP})
        saver = _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=17, session_id="calls_5"),
            job_result=_JobResult(mcp_init=None),
        )
        obs.McpHealthTelemetryObserver().execute(event)
        saver.assert_called_once_with(17, 5, None)
        sender.assert_not_called()  # connected is False is False for None
        assert event.verdict is None

    def test_both_null_still_updates_row(self, monkeypatch):
        _patch_config(monkeypatch, **{CFG_SEND_ALERT_ON_MCP_ISSUES: True, **_SMTP})
        monkeypatch.setattr(obs, "find_harness", lambda harness: None)
        saver = _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=18, session_id="any"),
            harness="unknown",
            job_result=_JobResult(mcp_init=None),
        )
        obs.McpHealthTelemetryObserver().execute(event)
        # UPDATE issued with (NULL, NULL) — overwrites any stale per-attempt values.
        saver.assert_called_once_with(18, None, None)
        sender.assert_not_called()
        assert event.verdict is None

    def test_retry_overwrites_prior_attempt_values(self, monkeypatch):
        """Same job row, two attempts: attempt 1 connected/3 → attempt 2 None/None.
        The observer recomputes per attempt and always writes both columns, so a
        prior attempt's values cannot survive into the next."""
        _patch_config(monkeypatch)
        saver = _patch_saver(monkeypatch)
        observer = obs.McpHealthTelemetryObserver()

        # Attempt 1: readable transcript w/ 3 toolbox calls + connected init.
        reader = _FakeReader({"s1": _summary(_toolbox(3))})
        monkeypatch.setattr(obs, "find_harness", lambda harness: _harness_with(reader))
        observer.execute(_FinalizeEvent(
            job=_Job(id=20, session_id="s1"),
            job_result=_JobResult(_mcp_init(("toolbox", "connected"))),
        ))

        # Attempt 2 (same row): no reader, no init report.
        monkeypatch.setattr(obs, "find_harness", lambda harness: None)
        observer.execute(_FinalizeEvent(
            job=_Job(id=20, session_id="s2"),
            harness="unknown",
            job_result=_JobResult(mcp_init=None),
        ))

        assert saver.call_args_list[0].args == (20, 3, True)
        assert saver.call_args_list[1].args == (20, None, None)

    def test_no_alert_when_smtp_unconfigured(self, fake_reader, monkeypatch):
        _patch_config(monkeypatch, **{
            CFG_SEND_ALERT_ON_MCP_ISSUES: True,
            CFG_ALERT_EMAIL_TO: "ops@example.com",
            CFG_ALERT_SMTP_HOST: "",  # host empty -> _smtp_config() is None
        })
        saver = _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=21, session_id="zero_calls"),
            job_result=_JobResult(_mcp_init(("toolbox", "failed"))),
        )
        obs.McpHealthTelemetryObserver().execute(event)  # must not raise
        saver.assert_called_once_with(21, 0, False)
        sender.assert_not_called()

    def test_alert_smtp_failure_logged_not_raised(self, fake_reader, monkeypatch, caplog):
        _patch_config(monkeypatch, **{CFG_SEND_ALERT_ON_MCP_ISSUES: True, **_SMTP})
        saver = _patch_saver(monkeypatch)

        def _boom(*_a, **_kw):
            raise OSError("smtp down")
        monkeypatch.setattr(obs, "send_alert", _boom)

        event = _FinalizeEvent(
            job=_Job(id=22, session_id="zero_calls"),
            job_result=_JobResult(_mcp_init(("toolbox", "failed"))),
        )
        with caplog.at_level(logging.WARNING, logger=obs.logger.name):
            obs.McpHealthTelemetryObserver().execute(event)  # returns cleanly

        saver.assert_called_once_with(22, 0, False)  # columns still persisted
        assert any("SMTP send failed" in r.message for r in caplog.records)

    def test_drift_logs_persists_null_calls_and_known_connected(self, fake_reader, monkeypatch, caplog):
        _patch_config(monkeypatch)
        saver = _patch_saver(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=23, session_id="drifted"),
            job_result=_JobResult(_mcp_init(("toolbox", "connected"))),
        )
        with caplog.at_level(logging.ERROR, logger=obs.logger.name):
            obs.McpHealthTelemetryObserver().execute(event)

        # calls NULL (parse unreliable) but connected TRUE (independent of transcript).
        saver.assert_called_once_with(23, None, True)
        assert event.verdict is None
        assert any("parser drift detected" in r.message for r in caplog.records)

    def test_toolbox_absent_from_init_list_is_false(self, monkeypatch):
        _patch_config(monkeypatch)
        monkeypatch.setattr(obs, "find_harness", lambda harness: None)
        saver = _patch_saver(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=24, session_id="any"),
            harness="unknown",
            job_result=_JobResult(_mcp_init(("context7", "connected"))),
        )
        obs.McpHealthTelemetryObserver().execute(event)
        # init present, toolbox not visible -> FALSE
        assert saver.call_args.args == (24, None, False)

    def test_empty_servers_list_is_false(self, monkeypatch):
        _patch_config(monkeypatch)
        monkeypatch.setattr(obs, "find_harness", lambda harness: None)
        saver = _patch_saver(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=25, session_id="any"),
            harness="unknown",
            job_result=_JobResult(_mcp_init()),  # servers=()
        )
        obs.McpHealthTelemetryObserver().execute(event)
        assert saver.call_args.args == (25, None, False)

    def test_toolbox_provider_lacks_init_is_null(self, monkeypatch):
        _patch_config(monkeypatch)
        monkeypatch.setattr(obs, "find_harness", lambda harness: None)
        saver = _patch_saver(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=26, session_id="any"),
            harness="unknown",
            job_result=_JobResult(mcp_init=None),  # provider exposed no init report
        )
        obs.McpHealthTelemetryObserver().execute(event)
        # NULL is distinct from FALSE.
        assert saver.call_args.args == (26, None, None)

    def test_pending_status_is_unknown_and_does_not_warn(
        self, fake_reader, monkeypatch, caplog,
    ):
        """`pending` = the CLI printed init before the handshake finished.

        It is the expected report on every job, so it must resolve to NULL
        ("we don't know") and must NOT emit a per-job warning.
        """
        _patch_config(monkeypatch, **{CFG_SEND_ALERT_ON_MCP_ISSUES: True, **_SMTP})
        saver = _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=30, session_id="calls_5"),
            job_result=_JobResult(_mcp_init(("toolbox", "pending"))),
        )
        with caplog.at_level("WARNING"):
            obs.McpHealthTelemetryObserver().execute(event)
        saver.assert_called_once_with(30, 5, None)
        sender.assert_not_called()
        assert caplog.text == ""

    def test_pending_status_with_zero_calls_alerts_on_calls_only(
        self, fake_reader, monkeypatch,
    ):
        _patch_config(monkeypatch, **{CFG_SEND_ALERT_ON_MCP_ISSUES: True, **_SMTP})
        saver = _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=31, session_id="zero_calls"),
            job_result=_JobResult(_mcp_init(("toolbox", "pending"))),
        )
        obs.McpHealthTelemetryObserver().execute(event)
        saver.assert_called_once_with(31, 0, None)
        sender.assert_called_once()
        _, _, subject, _ = sender.call_args.args
        assert "0 toolbox calls" in subject
        assert "toolbox not connected" not in subject

    def test_unrecognized_status_is_unknown_and_warns(
        self, fake_reader, monkeypatch, caplog,
    ):
        """A status word we have never seen is UNKNOWN, not "broken" — but loud."""
        _patch_config(monkeypatch, **{CFG_SEND_ALERT_ON_MCP_ISSUES: True, **_SMTP})
        saver = _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=32, session_id="calls_5"),
            job_result=_JobResult(_mcp_init(("toolbox", "connecting"))),
        )
        with caplog.at_level("WARNING"):
            obs.McpHealthTelemetryObserver().execute(event)
        saver.assert_called_once_with(32, 5, None)
        sender.assert_not_called()
        assert "connecting" in caplog.text

    @pytest.mark.parametrize("status", ["failed", "needs-auth", "needs-approval", "disabled"])
    def test_terminal_statuses_are_not_connected(self, status, fake_reader, monkeypatch):
        """The CLI decided this server will not serve tools -> FALSE, and alert
        regardless of the call count."""
        _patch_config(monkeypatch, **{CFG_SEND_ALERT_ON_MCP_ISSUES: True, **_SMTP})
        saver = _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=33, session_id="calls_5"),
            job_result=_JobResult(_mcp_init(("toolbox", status))),
        )
        obs.McpHealthTelemetryObserver().execute(event)
        saver.assert_called_once_with(33, 5, False)
        sender.assert_called_once()
        _, _, subject, _ = sender.call_args.args
        assert f"toolbox not connected ({status})" in subject

    @pytest.mark.parametrize(
        ("init", "expected"),
        [
            (_mcp_init(("toolbox", "failed")), "failed"),
            (_mcp_init(("context7", "connected")), "absent"),
            (_mcp_init(), "no-servers"),
        ],
    )
    def test_alert_names_the_raw_toolbox_status(
        self, init, expected, fake_reader, monkeypatch,
    ):
        """Ops must be able to see WHY from the email alone."""
        _patch_config(monkeypatch, **{CFG_SEND_ALERT_ON_MCP_ISSUES: True, **_SMTP})
        _patch_saver(monkeypatch)
        sender = _patch_sender(monkeypatch)
        event = _FinalizeEvent(
            job=_Job(id=34, session_id="calls_5"),
            job_result=_JobResult(init),
        )
        obs.McpHealthTelemetryObserver().execute(event)
        sender.assert_called_once()
        _, _, subject, body = sender.call_args.args
        assert f"toolbox not connected ({expected})" in subject
        assert f"Toolbox status: {expected}" in body

    def test_observer_never_raises(self, monkeypatch):
        observer = obs.McpHealthTelemetryObserver()

        # 1. _config() itself throws (covers _flag/_smtp_config too).
        def _bad_config():
            raise RuntimeError("config backend down")
        monkeypatch.setattr(obs, "_config", _bad_config)
        monkeypatch.setattr(obs, "find_harness", lambda harness: None)
        observer.execute(_FinalizeEvent(job=_Job(session_id="x")))

        # 2. reader.parse throws unexpectedly.
        _patch_config(monkeypatch)

        class _Broken:
            def parse(self, session_id):
                raise RuntimeError("disk corrupted")

            def iter_tool_uses(self, session_id):
                return self.parse(session_id)

        monkeypatch.setattr(obs, "find_harness", lambda harness: _harness_with(_Broken()))
        observer.execute(_FinalizeEvent(job=_Job(session_id="x")))

        # 3. persist throws.
        monkeypatch.setattr(obs, "find_harness", lambda harness: None)

        def _bad_save(*_a, **_kw):
            raise RuntimeError("db down")
        monkeypatch.setattr(obs, "_save_mcp_telemetry", _bad_save)
        observer.execute(_FinalizeEvent(
            job=_Job(session_id="x"),
            job_result=_JobResult(mcp_init=None),
        ))

        # 4. job_result is None entirely.
        observer.execute(_FinalizeEvent(job=_Job(session_id="x"), job_result=None))

    def test_flag_default_off_and_string_truthy(self, monkeypatch):
        monkeypatch.setattr(obs, "_config", lambda: {})
        assert obs._flag(CFG_SEND_ALERT_ON_MCP_ISSUES) is False  # missing key

        for truthy in ("true", "TRUE", "yes", "1", "on", "On"):
            monkeypatch.setattr(obs, "_config", lambda v=truthy: {CFG_SEND_ALERT_ON_MCP_ISSUES: v})
            assert obs._flag(CFG_SEND_ALERT_ON_MCP_ISSUES) is True, truthy

        for falsy in ("false", "0", "no", "off", "", "nonsense"):
            monkeypatch.setattr(obs, "_config", lambda v=falsy: {CFG_SEND_ALERT_ON_MCP_ISSUES: v})
            assert obs._flag(CFG_SEND_ALERT_ON_MCP_ISSUES) is False, falsy

        # Native bools pass straight through (config.json default is JSON false/true).
        monkeypatch.setattr(obs, "_config", lambda: {CFG_SEND_ALERT_ON_MCP_ISSUES: True})
        assert obs._flag(CFG_SEND_ALERT_ON_MCP_ISSUES) is True
        monkeypatch.setattr(obs, "_config", lambda: {CFG_SEND_ALERT_ON_MCP_ISSUES: False})
        assert obs._flag(CFG_SEND_ALERT_ON_MCP_ISSUES) is False


class TestAlertEmailObserver:
    def test_noop_when_no_email_to(self, monkeypatch):
        _patch_config(monkeypatch, **{
            CFG_ALERT_EMAIL_TO: "",
            CFG_ALERT_SMTP_HOST: "smtp.example.com",
        })
        sender = MagicMock()
        monkeypatch.setattr(obs, "send_alert", sender)
        obs.AlertEmailObserver().execute(_DeadEvent(job=_Job(), error=RuntimeError("x")))
        sender.assert_not_called()

    def test_noop_when_no_smtp_host(self, monkeypatch):
        _patch_config(monkeypatch, **{
            CFG_ALERT_EMAIL_TO: "ops@example.com",
            CFG_ALERT_SMTP_HOST: "",
        })
        sender = MagicMock()
        monkeypatch.setattr(obs, "send_alert", sender)
        obs.AlertEmailObserver().execute(_DeadEvent(job=_Job(), error=RuntimeError("x")))
        sender.assert_not_called()

    def test_sends_when_configured(self, monkeypatch):
        _patch_config(monkeypatch, **_SMTP)
        sender = MagicMock()
        monkeypatch.setattr(obs, "send_alert", sender)

        event = _DeadEvent(job=_Job(id=42, reference_id="AI-70"),
                           error=RuntimeError("boom"), elapsed_ms=500)
        obs.AlertEmailObserver().execute(event)

        sender.assert_called_once()
        smtp_cfg, to, subject, body = sender.call_args.args
        assert to == "ops@example.com"
        assert smtp_cfg.host == "smtp.example.com"
        assert smtp_cfg.tls is True
        assert "42" in subject
        assert "RuntimeError" in subject
        assert "AI-70" in body
        assert "boom" in body

    def test_body_includes_verdict_reason_and_detail_on_verification_failure(self, monkeypatch):
        """Alert email must surface verdict reason + detail when DEAD was
        triggered by a verification veto — ops needs the parser/agent
        distinction without opening a transcript."""
        _patch_config(monkeypatch, **{
            CFG_ALERT_EMAIL_TO: "ops@example.com",
            CFG_ALERT_SMTP_HOST: "smtp.example.com",
            CFG_ALERT_SMTP_PORT: 587,
            CFG_ALERT_SMTP_FROM: "agento@example.com",
            CFG_ALERT_SMTP_TLS: False,
        })
        sender = MagicMock()
        monkeypatch.setattr(obs, "send_alert", sender)

        verdict = Verdict(
            retryable=False,
            reason=VerifyReason.TRANSCRIPT_PARSE_FAILED,
            fresh_start=False,
            detail="parser recognized 0 of 18 JSON records — likely provider format change",
        )
        event = _DeadEvent(
            job=_Job(id=99, reference_id="AI-70"),
            error=JobVerificationFailed(verdict),
        )
        obs.AlertEmailObserver().execute(event)

        sender.assert_called_once()
        _, _, _, body = sender.call_args.args
        assert "transcript_parse_failed" in body
        assert "parser recognized 0 of 18" in body

    def test_smtp_failure_is_swallowed(self, monkeypatch):
        _patch_config(monkeypatch, **{
            CFG_ALERT_EMAIL_TO: "ops@example.com",
            CFG_ALERT_SMTP_HOST: "smtp.example.com",
            CFG_ALERT_SMTP_PORT: 587,
            CFG_ALERT_SMTP_FROM: "agento@example.com",
            CFG_ALERT_SMTP_TLS: False,
        })
        def _boom(*_a, **_kw):
            raise OSError("network down")
        monkeypatch.setattr(obs, "send_alert", _boom)
        obs.AlertEmailObserver().execute(
            _DeadEvent(job=_Job(), error=RuntimeError("x")),
        )


@dataclass
class _BreachEvent:
    channel: str = "outlook"
    reason: str = "dmarc_not_pass"
    sender: str | None = "sklep@example.com"
    reference_id: str | None = "AAMkAG-1"
    detail: str | None = "dmarc=fail"


class TestSecurityBreachAlertObserver:
    def test_noop_when_no_email_to(self, monkeypatch):
        _patch_config(monkeypatch, **{CFG_ALERT_EMAIL_TO: "", CFG_ALERT_SMTP_HOST: "smtp.example.com"})
        sender = MagicMock()
        monkeypatch.setattr(obs, "send_alert", sender)
        obs.SecurityBreachAlertObserver().execute(_BreachEvent())
        sender.assert_not_called()

    def test_noop_when_no_smtp_host(self, monkeypatch):
        _patch_config(monkeypatch, **{CFG_ALERT_EMAIL_TO: "ops@example.com", CFG_ALERT_SMTP_HOST: ""})
        sender = MagicMock()
        monkeypatch.setattr(obs, "send_alert", sender)
        obs.SecurityBreachAlertObserver().execute(_BreachEvent())
        sender.assert_not_called()

    def test_sends_with_channel_reason_and_sender_when_configured(self, monkeypatch):
        _patch_config(monkeypatch, **_SMTP)
        sender = MagicMock()
        monkeypatch.setattr(obs, "send_alert", sender)
        obs.SecurityBreachAlertObserver().execute(_BreachEvent())
        sender.assert_called_once()
        _smtp_cfg, to, subject, body = sender.call_args.args
        assert to == "ops@example.com"
        assert "outlook" in subject
        assert "dmarc_not_pass" in subject
        assert "sklep@example.com" in body
        assert "AAMkAG-1" in body

    def test_smtp_failure_is_swallowed(self, monkeypatch):
        _patch_config(monkeypatch, **_SMTP)

        def _boom(*_a, **_kw):
            raise OSError("network down")
        monkeypatch.setattr(obs, "send_alert", _boom)
        obs.SecurityBreachAlertObserver().execute(_BreachEvent())  # must not raise


@dataclass
class _StallEvent:
    channel: str = "outlook"
    mailbox: str = "shared@example.com"
    reason: str = "no_bindings"
    detail: str | None = "routed mode but no active outlook_sender bindings"


class TestMailboxStalledAlertObserver:
    def test_noop_when_no_email_to(self, monkeypatch):
        _patch_config(monkeypatch, **{CFG_ALERT_EMAIL_TO: "", CFG_ALERT_SMTP_HOST: "smtp.example.com"})
        sender = MagicMock()
        monkeypatch.setattr(obs, "send_alert", sender)
        obs.MailboxStalledAlertObserver().execute(_StallEvent())
        sender.assert_not_called()

    def test_noop_when_no_smtp_host(self, monkeypatch):
        _patch_config(monkeypatch, **{CFG_ALERT_EMAIL_TO: "ops@example.com", CFG_ALERT_SMTP_HOST: ""})
        sender = MagicMock()
        monkeypatch.setattr(obs, "send_alert", sender)
        obs.MailboxStalledAlertObserver().execute(_StallEvent())
        sender.assert_not_called()

    def test_sends_with_mailbox_reason_and_explanation_when_configured(self, monkeypatch):
        _patch_config(monkeypatch, **_SMTP)
        sender = MagicMock()
        monkeypatch.setattr(obs, "send_alert", sender)
        obs.MailboxStalledAlertObserver().execute(_StallEvent())
        sender.assert_called_once()
        _smtp_cfg, to, subject, body = sender.call_args.args
        assert to == "ops@example.com"
        assert "shared@example.com" in subject
        assert "no_bindings" in subject
        assert "outlook" in body
        assert "no_bindings" in body
        # the reason code is expanded into a human-readable explanation for ops
        assert "dropped" in body.lower()

    def test_unknown_reason_still_sends_generic_body(self, monkeypatch):
        _patch_config(monkeypatch, **_SMTP)
        sender = MagicMock()
        monkeypatch.setattr(obs, "send_alert", sender)
        obs.MailboxStalledAlertObserver().execute(_StallEvent(reason="brand_new_reason"))
        sender.assert_called_once()
        _smtp_cfg, _to, subject, _body = sender.call_args.args
        assert "brand_new_reason" in subject

    def test_smtp_failure_is_swallowed(self, monkeypatch):
        _patch_config(monkeypatch, **_SMTP)

        def _boom(*_a, **_kw):
            raise OSError("network down")
        monkeypatch.setattr(obs, "send_alert", _boom)
        obs.MailboxStalledAlertObserver().execute(_StallEvent())  # must not raise
