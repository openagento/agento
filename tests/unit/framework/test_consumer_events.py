"""Tests for event dispatching from Consumer."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.consumer import Consumer, _JobResult
from agento.framework.event_manager import ObserverEntry, get_event_manager
from agento.framework.event_manager import clear as clear_event_manager
from agento.framework.events import (
    JobBlockedEvent,
    JobClaimedEvent,
    JobDeadEvent,
    JobFailedEvent,
    JobFinalizeEvent,
    JobRetryingEvent,
    JobSucceededEvent,
    Verdict,
    VerifyReason,
)
from agento.framework.job_models import AgentType, Job


@pytest.fixture(autouse=True)
def _clean():
    clear_event_manager()
    yield
    clear_event_manager()


def _make_job(**overrides) -> Job:
    job = Job.stub(type=AgentType.CRON, source="jira", reference_id="TEST-1")
    job.id = overrides.get("id", 1)
    for k, v in overrides.items():
        setattr(job, k, v)
    return job


def _mock_configs():
    from agento.framework.consumer_config import ConsumerConfig
    from agento.framework.database_config import DatabaseConfig
    db = DatabaseConfig()
    consumer = ConsumerConfig(job_timeout_seconds=60, disable_llm=True)
    return db, consumer


class _EventCollector:
    """Observer that collects dispatched events."""

    events: list = []  # noqa: RUF012

    def execute(self, event: object) -> None:
        _EventCollector.events.append(event)

    @classmethod
    def reset(cls):
        cls.events = []


def _running(mock_conn):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {"status": "RUNNING"}
    mock_conn.return_value = conn
    return conn


def _consumer():
    return Consumer(*_mock_configs(), MagicMock())


@pytest.fixture(autouse=True)
def _reset_collector():
    _EventCollector.reset()
    yield


class TestFinalizeJobEvents:
    @patch("agento.framework.consumer.get_connection")
    def test_success_dispatches_job_succeeded(self, mock_conn):
        _running(mock_conn)
        em = get_event_manager()
        em.register("job_succeed_after", ObserverEntry(name="col", observer_class=_EventCollector))

        consumer = _consumer()
        job = _make_job()
        result = _JobResult(summary="done", agent_type="claude", model="opus")

        consumer._finalize_job(job, None, result, 500)

        assert len(_EventCollector.events) == 1
        evt = _EventCollector.events[0]
        assert isinstance(evt, JobSucceededEvent)
        assert evt.job is job
        assert evt.summary == "done"
        assert evt.elapsed_ms == 500

    @patch("agento.framework.consumer.get_connection")
    def test_non_retryable_failure_dispatches_failed_and_dead(self, mock_conn):
        _running(mock_conn)
        em = get_event_manager()
        em.register("job_fail_after", ObserverEntry(name="f", observer_class=_EventCollector))
        em.register("job_dead_after", ObserverEntry(name="d", observer_class=_EventCollector))

        consumer = _consumer()
        # ValueError is non-retryable per retry_policy
        job = _make_job(attempt=1, max_attempts=3)
        error = ValueError("bad input")

        consumer._finalize_job(job, error, None, 100)

        types = [type(e) for e in _EventCollector.events]
        assert JobFailedEvent in types
        assert JobDeadEvent in types


class TestJobFinalizeEvents:
    """Coverage for the verification-gate events added with app_monitor."""

    @patch("agento.framework.consumer.get_connection")
    def test_success_with_no_verdict_dispatches_before_and_after(self, mock_conn):
        _running(mock_conn)
        em = get_event_manager()
        em.register("job_finalize_before", ObserverEntry(name="b", observer_class=_EventCollector))
        em.register("job_finalize_after", ObserverEntry(name="a", observer_class=_EventCollector))

        consumer = _consumer()
        job = _make_job()
        result = _JobResult(summary="done")

        consumer._finalize_job(job, None, result, 250)

        types = [type(e) for e in _EventCollector.events]
        assert types.count(JobFinalizeEvent) == 2  # before + after, same payload
        for e in _EventCollector.events:
            assert e.verdict is None
            assert e.job is job
            assert e.job_result is result

    @patch("agento.framework.consumer.get_connection")
    def test_veto_retryable_clears_session_and_routes_to_retry(self, mock_conn):
        _conn = _running(mock_conn)

        class _Vetoer:
            def execute(self, event):
                event.verdict = Verdict(
                    retryable=True,
                    reason=VerifyReason.NO_MCP_CALLS,
                    fresh_start=True,
                    detail="zero mcp__toolbox__ calls",
                )

        em = get_event_manager()
        em.register("job_finalize_before", ObserverEntry(name="v", observer_class=_Vetoer))
        em.register("job_finalize_after", ObserverEntry(name="a", observer_class=_EventCollector))
        em.register("job_fail_after", ObserverEntry(name="f", observer_class=_EventCollector))
        em.register("job_retry_after", ObserverEntry(name="r", observer_class=_EventCollector))

        consumer = _consumer()
        job = _make_job(attempt=1, max_attempts=3)
        result = _JobResult(summary="rc=0 but no MCP", session_id="sess-42")

        consumer._finalize_job(job, None, result, 100)

        types = [type(e) for e in _EventCollector.events]
        assert JobFailedEvent in types  # vetoed run treated as failure
        assert JobRetryingEvent in types  # retryable veto → re-queued
        assert JobDeadEvent not in types

        finalize_after = next(e for e in _EventCollector.events if isinstance(e, JobFinalizeEvent))
        assert finalize_after.verdict is not None
        assert finalize_after.verdict.reason == VerifyReason.NO_MCP_CALLS
        assert finalize_after.verdict.fresh_start is True

        # Confirm session_id was cleared via a dedicated UPDATE (the fix for incident 3368).
        executed_sql = [
            call.args[0]
            for call in _conn.cursor.return_value.__enter__.return_value.execute.call_args_list
        ]
        assert any("session_id = NULL" in sql for sql in executed_sql)

    @patch("agento.framework.consumer.get_connection")
    def test_veto_non_retryable_routes_to_dead(self, mock_conn):
        _running(mock_conn)

        class _Vetoer:
            def execute(self, event):
                event.verdict = Verdict(
                    retryable=False,
                    reason=VerifyReason.TRANSCRIPT_MISSING,
                    fresh_start=False,
                )

        em = get_event_manager()
        em.register("job_finalize_before", ObserverEntry(name="v", observer_class=_Vetoer))
        em.register("job_finalize_after", ObserverEntry(name="a", observer_class=_EventCollector))
        em.register("job_dead_after", ObserverEntry(name="d", observer_class=_EventCollector))
        em.register("job_retry_after", ObserverEntry(name="r", observer_class=_EventCollector))

        consumer = _consumer()
        job = _make_job(attempt=1, max_attempts=3)

        consumer._finalize_job(job, None, _JobResult(summary="x"), 50)

        types = [type(e) for e in _EventCollector.events]
        assert JobDeadEvent in types
        assert JobRetryingEvent not in types

        finalize_after = next(e for e in _EventCollector.events if isinstance(e, JobFinalizeEvent))
        assert finalize_after.verdict is not None
        assert finalize_after.verdict.reason == VerifyReason.TRANSCRIPT_MISSING

    @patch("agento.framework.consumer.get_connection")
    def test_veto_blocked_routes_to_failed_not_dead(self, mock_conn):
        """A blocked verdict (config/infra fault) must halt WITHOUT retry and
        route to FAILED + job_blocked_after — never DEAD, never a retry — even
        though the verdict is also marked ``retryable``. FAILED (the otherwise
        unused status) keeps DEAD reserved for genuine agent exhaustion."""
        _running(mock_conn)

        class _Vetoer:
            def execute(self, event):
                event.verdict = Verdict(
                    retryable=True,
                    reason=VerifyReason.MISCONFIGURED,
                    blocked=True,
                    detail="toolbox MCP credential missing",
                )

        em = get_event_manager()
        em.register("job_finalize_before", ObserverEntry(name="v", observer_class=_Vetoer))
        em.register("job_finalize_after", ObserverEntry(name="a", observer_class=_EventCollector))
        em.register("job_blocked_after", ObserverEntry(name="b", observer_class=_EventCollector))
        em.register("job_dead_after", ObserverEntry(name="d", observer_class=_EventCollector))
        em.register("job_retry_after", ObserverEntry(name="r", observer_class=_EventCollector))

        consumer = _consumer()
        job = _make_job(attempt=1, max_attempts=3)

        consumer._finalize_job(job, None, _JobResult(summary="x"), 50)

        types = [type(e) for e in _EventCollector.events]
        assert JobBlockedEvent in types
        assert JobDeadEvent not in types
        assert JobRetryingEvent not in types  # status FAILED: test_finalize_branch_pins


class TestDequeueEvents:
    @patch("agento.framework.consumer.get_connection")
    def test_dequeue_dispatches_job_claimed(self, mock_get_conn):
        from datetime import datetime

        em = get_event_manager()
        em.register("job_claim_after", ObserverEntry(name="c", observer_class=_EventCollector))

        now = datetime.now(UTC)
        row = {
            "id": 1, "schedule_id": None, "type": "cron", "source": "jira",
            "agent_view_id": None, "priority": 50,
            "reference_id": "TEST-1", "agent_type": None, "model": None,
            "input_tokens": None, "output_tokens": None, "prompt": None,
            "output": None, "context": None,
            "status": "TODO", "attempt": 0, "max_attempts": 3,
            "scheduled_after": now, "started_at": None,
            "finished_at": None, "result_summary": None,
            "error_message": None, "error_class": None,
            "idempotency_key": "key-1",
            "created_at": now, "updated_at": now,
        }

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = row

        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        db, consumer_config = _mock_configs()
        consumer = Consumer(db, consumer_config, MagicMock())
        job = consumer._try_dequeue()

        assert job is not None
        assert len(_EventCollector.events) == 1
        assert isinstance(_EventCollector.events[0], JobClaimedEvent)


# Pins every transition that ends an attempt (WS2, SEC-7): the job status, the attempt
# refund, the capability revoke, the job.failed outbox row, the execution outcome and
# the events. Pool-wait and blocked KEEP the capabilities; retry and dead revoke.
_FINALIZE_BRANCHES = {
    # name: (status, refund, revoke, outbox, outcome, terminal, event)
    "pool_wait": ("TODO", True, False, False, "abandoned", False, JobRetryingEvent),
    "retry": ("TODO", False, True, True, "failed", False, JobRetryingEvent),
    "blocked": ("FAILED", False, False, True, "failed", True, JobBlockedEvent),
    "dead": ("DEAD", False, True, True, "failed", True, JobDeadEvent),
}


def _branch_error(name):
    from datetime import datetime, timedelta

    from agento.framework.events import JobVerificationFailed

    if name == "pool_wait":
        error = RuntimeError("pool dry")
        error.pool_retry_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)  # type: ignore[attr-defined]
        return error, 1
    if name == "blocked":
        return JobVerificationFailed(Verdict(retryable=True, reason=VerifyReason.MISCONFIGURED, blocked=True)), 1
    return RuntimeError(name), 3 if name == "dead" else 1


def _pin_transition(conn, mock_revoke, mock_finalize, mock_outbox, status, outbox, outcome, terminal):
    """The one ``UPDATE job`` and what goes with it; returns that UPDATE."""
    cursor = conn.cursor.return_value.__enter__.return_value
    [update] = [c for c in cursor.execute.call_args_list if c.args[0].lstrip().startswith("UPDATE job")]
    assert f"status = '{status}'" in update.args[0]
    assert [c.kwargs["kind"] for c in mock_outbox.call_args_list] == (["job.failed"] if outbox else [])
    assert mock_finalize.call_args.kwargs["outcome"] == outcome
    assert mock_finalize.call_args.kwargs["job_terminal"] is terminal
    conn.commit.assert_called_once()
    return update


def _collect_job_events():
    for ev in ("job_fail_after", "job_retry_after", "job_blocked_after", "job_dead_after"):
        get_event_manager().register(ev, ObserverEntry(name=ev, observer_class=_EventCollector))


@pytest.mark.parametrize("name", list(_FINALIZE_BRANCHES))
@patch("agento.framework.consumer.write_outbox")
@patch("agento.framework.consumer.finalize_execution")
@patch("agento.framework.consumer.revoke_job_capabilities")
@patch("agento.framework.consumer.get_connection")
def test_finalize_branch_pins(mock_conn, mock_revoke, mock_finalize, mock_outbox, name):
    status, refund, revoke, outbox, outcome, terminal, event_cls = _FINALIZE_BRANCHES[name]
    conn = _running(mock_conn)
    _collect_job_events()

    error, attempt = _branch_error(name)
    _consumer()._finalize_job(_make_job(attempt=attempt, max_attempts=3), error, None, 10)

    update = _pin_transition(conn, mock_revoke, mock_finalize, mock_outbox, status, outbox, outcome, terminal)
    assert ("GREATEST(attempt - 1, 0)" in update.args[0]) is refund
    assert mock_revoke.called is revoke
    assert [type(e) for e in _EventCollector.events] == [JobFailedEvent, event_cls]


@pytest.mark.parametrize(("attempt", "status", "outbox", "outcome", "terminal"), [
    (1, "TODO", False, "abandoned", False),
    (3, "DEAD", True, "failed", True),
])
@patch("agento.framework.consumer.write_outbox")
@patch("agento.framework.consumer.finalize_execution")
@patch("agento.framework.consumer.revoke_job_capabilities")
@patch("agento.framework.consumer.get_connection")
def test_stale_recovery_branch_pins(mock_conn, mock_revoke, mock_finalize, mock_outbox,
                                    attempt, status, outbox, outcome, terminal):
    conn = mock_conn.return_value
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        {"id": 7, "reference_id": "R", "pid": 99999, "runner_ref": "runner-1.sock:b1",
         "attempt": attempt, "max_attempts": 3, "started_at": None}]
    _collect_job_events()

    with patch("agento.framework.consumer.runner_client.alive", return_value="dead"):
        _consumer()._recover_stale_jobs()

    update = _pin_transition(conn, mock_revoke, mock_finalize, mock_outbox, status, outbox, outcome, terminal)
    assert "GREATEST" not in update.args[0]
    assert "StaleJobRecovery" in update.args[0] or "StaleJobRecovery" in update.args[1]
    mock_revoke.assert_called_once()
    assert _EventCollector.events == []  # recovery dispatches no job event
