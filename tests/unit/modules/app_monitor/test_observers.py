from __future__ import annotations

import logging
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from agento.framework.events import JobVerificationFailed, Verdict, VerifyReason
from agento.framework.harness import McpInitReport, McpServerStatus
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


@dataclass
class _BlockedEvent:
    job: _Job
    error: Exception
    elapsed_ms: int = 0


def _mcp_init(*pairs: tuple[str, str]) -> McpInitReport:
    return McpInitReport(servers=tuple(McpServerStatus(n, s) for n, s in pairs))


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


# Toolbox call count per session id; an id not listed is "unknown" (NULL).
_COUNTS = {"calls_3": 3, "calls_5": 5, "zero_calls": 0}


@pytest.fixture(autouse=True)
def fake_counts(monkeypatch):
    """No DB: the count is keyed off the job's session id, the connection is a mock.
    The real query is pinned in ``test_toolbox_call_count.py``."""
    monkeypatch.setattr(obs, "get_connection", lambda _cfg: MagicMock())
    monkeypatch.setattr(
        obs, "_count_toolbox_calls", lambda _conn, job: _COUNTS.get(job.session_id),
    )


def _patch_config(monkeypatch, **kwargs):
    monkeypatch.setattr(obs, "_config", lambda: kwargs)


def _patch_saver(monkeypatch) -> MagicMock:
    """Records ``(job_id, calls, connected)``; the connection argument is dropped."""
    saver = MagicMock()
    monkeypatch.setattr(obs, "_save_mcp_telemetry", lambda _conn, *a: saver(*a))
    return saver


def _patch_sender(monkeypatch) -> MagicMock:
    sender = MagicMock()
    monkeypatch.setattr(obs, "send_alert", sender)
    return sender


class TestMcpHealthTelemetry:
    """Telemetry truth table — dual nullable signals, combined alert, no verdict."""

    def test_persists_both_signals_connected_with_calls(self, monkeypatch):
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

    def test_persists_connected_zero_calls_no_alert(self, monkeypatch):
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

    def test_persists_connected_zero_calls_alerts(self, monkeypatch):
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

    def test_persists_not_connected_with_calls_alerts(self, monkeypatch):
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

    def test_persists_not_connected_no_calls_alerts_once(self, monkeypatch):
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

    def test_persists_no_init_data_with_calls(self, monkeypatch):
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

    def test_null_connected_does_not_alert(self, monkeypatch):
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

        # Attempt 1: 3 audited toolbox calls + connected init.
        observer.execute(_FinalizeEvent(
            job=_Job(id=20, session_id="calls_3"),
            job_result=_JobResult(_mcp_init(("toolbox", "connected"))),
        ))

        # Attempt 2 (same row): count unknown, no init report.
        observer.execute(_FinalizeEvent(
            job=_Job(id=20, session_id="s2"),
            harness="unknown",
            job_result=_JobResult(mcp_init=None),
        ))

        assert saver.call_args_list[0].args == (20, 3, True)
        assert saver.call_args_list[1].args == (20, None, None)

    def test_no_alert_when_smtp_unconfigured(self, monkeypatch):
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

    def test_alert_smtp_failure_logged_not_raised(self, monkeypatch, caplog):
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

    def test_toolbox_absent_from_init_list_is_false(self, monkeypatch):
        _patch_config(monkeypatch)
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
        self, monkeypatch, caplog,
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
        self, monkeypatch,
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
        self, monkeypatch, caplog,
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
    def test_terminal_statuses_are_not_connected(self, status, monkeypatch):
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
        self, init, expected, monkeypatch,
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
        observer.execute(_FinalizeEvent(job=_Job(session_id="x")))

        # 2. the DB connection cannot be opened.
        _patch_config(monkeypatch)

        def _no_db(_cfg):
            raise RuntimeError("db down")
        monkeypatch.setattr(obs, "get_connection", _no_db)
        observer.execute(_FinalizeEvent(job=_Job(session_id="x")))
        monkeypatch.setattr(obs, "get_connection", lambda _cfg: MagicMock())

        # 3. persist throws.

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


class TestJobBlockedAlertObserver:
    def test_noop_when_no_email_to(self, monkeypatch):
        _patch_config(monkeypatch, **{
            CFG_ALERT_EMAIL_TO: "",
            CFG_ALERT_SMTP_HOST: "smtp.example.com",
        })
        sender = MagicMock()
        monkeypatch.setattr(obs, "send_alert", sender)
        obs.JobBlockedAlertObserver().execute(_BlockedEvent(job=_Job(), error=RuntimeError("x")))
        sender.assert_not_called()

    def test_noop_when_no_smtp_host(self, monkeypatch):
        _patch_config(monkeypatch, **{
            CFG_ALERT_EMAIL_TO: "ops@example.com",
            CFG_ALERT_SMTP_HOST: "",
        })
        sender = MagicMock()
        monkeypatch.setattr(obs, "send_alert", sender)
        obs.JobBlockedAlertObserver().execute(_BlockedEvent(job=_Job(), error=RuntimeError("x")))
        sender.assert_not_called()

    def test_sends_config_framed_alert_with_verdict(self, monkeypatch):
        """The alert must name the job, frame the cause as configuration/
        infrastructure (not agent), and surface verdict reason + detail."""
        _patch_config(monkeypatch, **_SMTP)
        sender = MagicMock()
        monkeypatch.setattr(obs, "send_alert", sender)

        verdict = Verdict(
            retryable=True,
            reason=VerifyReason.MISCONFIGURED,
            blocked=True,
            detail="toolbox MCP credential missing",
        )
        event = _BlockedEvent(
            job=_Job(id=585254, reference_id="AI-70"),
            error=JobVerificationFailed(verdict),
        )
        obs.JobBlockedAlertObserver().execute(event)

        sender.assert_called_once()
        _, to, subject, body = sender.call_args.args
        assert to == "ops@example.com"
        assert "585254" in subject
        assert "configuration/infrastructure fault" in subject
        assert "NOT an agent failure" in body
        assert "misconfigured" in body
        assert "toolbox MCP credential missing" in body

    def test_smtp_failure_is_swallowed(self, monkeypatch):
        _patch_config(monkeypatch, **_SMTP)
        def _boom(*_a, **_kw):
            raise OSError("network down")
        monkeypatch.setattr(obs, "send_alert", _boom)
        obs.JobBlockedAlertObserver().execute(
            _BlockedEvent(job=_Job(), error=RuntimeError("x")),
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
