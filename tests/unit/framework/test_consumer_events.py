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


@pytest.fixture(autouse=True)
def _reset_collector():
    _EventCollector.reset()
    yield


class TestFinalizeJobEvents:
    def _make_consumer(self):
        db, consumer = _mock_configs()
        return Consumer(db, consumer, MagicMock())

    @patch("agento.framework.consumer.get_connection")
    def test_success_dispatches_job_succeeded(self, mock_conn):
        _conn = MagicMock()
        _conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ("RUNNING",)
        mock_conn.return_value = _conn
        em = get_event_manager()
        em.register("job_succeed_after", ObserverEntry(name="col", observer_class=_EventCollector))

        consumer = self._make_consumer()
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
    def test_retryable_failure_dispatches_failed_and_retrying(self, mock_conn):
        _conn = MagicMock()
        _conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ("RUNNING",)
        mock_conn.return_value = _conn
        em = get_event_manager()
        em.register("job_fail_after", ObserverEntry(name="f", observer_class=_EventCollector))
        em.register("job_retry_after", ObserverEntry(name="r", observer_class=_EventCollector))

        consumer = self._make_consumer()
        job = _make_job(attempt=1, max_attempts=3)
        error = RuntimeError("transient")

        consumer._finalize_job(job, error, None, 100)

        types = [type(e) for e in _EventCollector.events]
        assert JobFailedEvent in types
        assert JobRetryingEvent in types

    @patch("agento.framework.consumer.get_connection")
    def test_non_retryable_failure_dispatches_failed_and_dead(self, mock_conn):
        _conn = MagicMock()
        _conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ("RUNNING",)
        mock_conn.return_value = _conn
        em = get_event_manager()
        em.register("job_fail_after", ObserverEntry(name="f", observer_class=_EventCollector))
        em.register("job_dead_after", ObserverEntry(name="d", observer_class=_EventCollector))

        consumer = self._make_consumer()
        # ValueError is non-retryable per retry_policy
        job = _make_job(attempt=1, max_attempts=3)
        error = ValueError("bad input")

        consumer._finalize_job(job, error, None, 100)

        types = [type(e) for e in _EventCollector.events]
        assert JobFailedEvent in types
        assert JobDeadEvent in types


class TestJobFinalizeEvents:
    """Coverage for the verification-gate events added with app_monitor."""

    def _make_consumer(self):
        db, consumer = _mock_configs()
        return Consumer(db, consumer, MagicMock())

    @patch("agento.framework.consumer.get_connection")
    def test_success_with_no_verdict_dispatches_before_and_after(self, mock_conn):
        _conn = MagicMock()
        _conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ("RUNNING",)
        mock_conn.return_value = _conn
        em = get_event_manager()
        em.register("job_finalize_before", ObserverEntry(name="b", observer_class=_EventCollector))
        em.register("job_finalize_after", ObserverEntry(name="a", observer_class=_EventCollector))

        consumer = self._make_consumer()
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
        _conn = MagicMock()
        _conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ("RUNNING",)
        mock_conn.return_value = _conn

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

        consumer = self._make_consumer()
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
        _conn = MagicMock()
        _conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ("RUNNING",)
        mock_conn.return_value = _conn

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

        consumer = self._make_consumer()
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
        _conn = MagicMock()
        _conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ("RUNNING",)
        mock_conn.return_value = _conn

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

        consumer = self._make_consumer()
        job = _make_job(attempt=1, max_attempts=3)

        consumer._finalize_job(job, None, _JobResult(summary="x"), 50)

        types = [type(e) for e in _EventCollector.events]
        assert JobBlockedEvent in types
        assert JobDeadEvent not in types
        assert JobRetryingEvent not in types

        # The terminal UPDATE writes status = 'FAILED', not 'DEAD'.
        executed_sql = [
            call.args[0]
            for call in _conn.cursor.return_value.__enter__.return_value.execute.call_args_list
        ]
        assert any("status = 'FAILED'" in sql for sql in executed_sql)
        assert not any("status = 'DEAD'" in sql for sql in executed_sql)


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
