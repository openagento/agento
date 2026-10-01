"""Every transition that ends an attempt calls the finalizer exactly once (§5.3).

One test per row of §5.3's table, against a real database and the real consumer. Two rows
of that table belong to PRD E7 §4.3.1 and are deliberately absent: the **stop-request pass**
(`job_stop_request` does not exist yet - `job_store.py` writes no such row) and the
**stale-worker abort** (the wrapped runner's attempt re-check). E7 completes the table; the
tests here are written so it plugs in without changing them.
"""
from __future__ import annotations

import json
import logging

import pytest

from agento.framework.consumer import Consumer, _JobResult, failure_kind
from agento.framework.execution_hooks import clear as clear_hooks
from agento.framework.execution_hooks import register_execution_finalizer
from agento.framework.job_models import Job

from .conftest import _test_connection, fetch_job


class Recorder:
    """The module side of the seam, reduced to what the framework promises it."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def finalize(self, *, conn, job_id, attempt, execution_id, outcome, job_terminal) -> None:
        assert conn is not None, "the framework hands over its OPEN connection"
        self.calls.append({"job_id": job_id, "attempt": attempt, "execution_id": execution_id,
                           "outcome": outcome, "job_terminal": job_terminal})

    @property
    def outcomes(self) -> list[tuple[str, bool]]:
        return [(c["outcome"], c["job_terminal"]) for c in self.calls]


@pytest.fixture
def recorder():
    clear_hooks()
    r = Recorder()
    register_execution_finalizer(r, module="test")
    yield r
    clear_hooks()


@pytest.fixture
def conn():
    c = _test_connection(autocommit=False)
    with c.cursor() as cur:
        for table in ("job_event_outbox", "toolbox_capability", "job"):
            cur.execute(f"DELETE FROM {table}")
    c.commit()
    yield c
    c.close()


def _insert(conn, *, status="RUNNING", attempt=1, max_attempts=3, key="fin:1") -> Job:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job (type, source, reference_id, idempotency_key, status, attempt, "
            "max_attempts, started_at) VALUES ('blank', 'test', 'F-1', %s, %s, %s, %s, NOW())",
            (key, status, attempt, max_attempts),
        )
        job_id = cur.lastrowid
        cur.execute("SELECT * FROM job WHERE id = %s", (job_id,))
        row = cur.fetchone()
    conn.commit()
    job = Job.from_row(row)
    job.execution_id = "exec-under-test"
    return job


def _consumer(int_db_config, int_consumer_config) -> Consumer:
    return Consumer(int_db_config, int_consumer_config, logging.getLogger("test"))


def _outbox(conn) -> list[dict]:
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT kind, payload, execution_id FROM job_event_outbox ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    return rows


class Boom(RuntimeError):
    pass


# --- §5.3's table, one test per row -----------------------------------------

def test_success_finalizes_the_attempt_and_ends_the_job(
    conn, recorder, int_db_config, int_consumer_config
):
    job = _insert(conn)

    _consumer(int_db_config, int_consumer_config)._finalize_job(
        job, error=None, job_result=_JobResult(summary="ok"), elapsed_ms=10)

    assert recorder.outcomes == [("succeeded", True)]
    assert recorder.calls[0]["execution_id"] == "exec-under-test"
    assert fetch_job(job.id)["status"] == "SUCCESS"


def test_a_retried_attempt_is_finalized_while_its_job_returns_to_todo(
    conn, recorder, int_db_config, int_consumer_config
):
    job = _insert(conn, attempt=1, max_attempts=3)

    _consumer(int_db_config, int_consumer_config)._finalize_job(
        job, error=Boom("transient"), job_result=None, elapsed_ms=10)

    assert recorder.outcomes == [("failed", False)]
    assert fetch_job(job.id)["status"] == "TODO"


def test_a_dead_letter_finalizes_the_attempt_and_the_job(
    conn, recorder, int_db_config, int_consumer_config
):
    job = _insert(conn, attempt=3, max_attempts=3)

    _consumer(int_db_config, int_consumer_config)._finalize_job(
        job, error=Boom("fatal"), job_result=None, elapsed_ms=10)

    assert recorder.outcomes == [("failed", True)]
    assert fetch_job(job.id)["status"] == "DEAD"


def test_a_pool_wait_abandons_the_attempt_and_refunds_it(
    conn, recorder, int_db_config, int_consumer_config
):
    from datetime import UTC, datetime, timedelta

    from agento.framework.agent_manager.errors import CredentialsBusyError

    job = _insert(conn, attempt=1)
    error = CredentialsBusyError(
        "pool busy",
        pool_retry_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=30),
    )

    _consumer(int_db_config, int_consumer_config)._finalize_job(
        job, error=error, job_result=None, elapsed_ms=10)

    assert recorder.outcomes == [("abandoned", False)]
    row = fetch_job(job.id)
    assert row["status"] == "TODO" and row["attempt"] == 0     # the attempt was refunded


def test_the_capability_mint_abort_abandons_the_attempt(
    conn, recorder, int_db_config, int_consumer_config, int_agent_view
):
    """The pre-spawn status re-check: a CLI pause landed between claim and mint."""
    job = _insert(conn, status="PAUSED")
    job.agent_view_id = int_agent_view

    assert _consumer(int_db_config, int_consumer_config)._issue_run_capabilities(
        conn, job) is None

    assert recorder.outcomes == [("abandoned", False)]
    assert fetch_job(job.id)["status"] == "PAUSED"             # the job row is untouched


def test_a_run_that_ends_after_a_pause_abandons_the_attempt(
    conn, recorder, int_db_config, int_consumer_config
):
    """The monitor path: the process exited, but the job is no longer ours to finalize."""
    job = _insert(conn, status="RUNNING")
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET status = 'PAUSED' WHERE id = %s", (job.id,))
    conn.commit()

    _consumer(int_db_config, int_consumer_config)._finalize_job(
        job, error=None, job_result=_JobResult(summary="ok"), elapsed_ms=10)

    assert recorder.outcomes == [("abandoned", False)]
    assert fetch_job(job.id)["status"] == "PAUSED"


def test_pause_job_itself_finalizes_nothing(conn, recorder):
    """A CLI-paused run whose process is still alive must NOT be finalized: `pause_job`
    stops waiting after a few seconds and cannot know the process is gone."""
    from agento.framework.job_store import pause_job

    job = _insert(conn, status="RUNNING")

    pause_job(conn, job.id)

    assert recorder.calls == []
    assert fetch_job(job.id)["status"] == "PAUSED"


def test_stale_recovery_abandons_the_attempt_it_reclaims(
    conn, recorder, int_db_config, int_consumer_config
):
    job = _insert(conn, attempt=1, max_attempts=3)
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET started_at = NOW() - INTERVAL 1 DAY WHERE id = %s",
                    (job.id,))
    conn.commit()

    _consumer(int_db_config, int_consumer_config)._recover_stale_jobs()

    assert recorder.outcomes == [("abandoned", False)]
    # It never held the other process's id, so the finalizer resolves it by job+attempt.
    assert recorder.calls[0] == {"job_id": job.id, "attempt": 1, "execution_id": None,
                                 "outcome": "abandoned", "job_terminal": False}
    assert fetch_job(job.id)["status"] == "TODO"


def test_stale_recovery_at_max_attempts_finalizes_and_ends_the_job(
    conn, recorder, int_db_config, int_consumer_config
):
    job = _insert(conn, attempt=3, max_attempts=3)
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET started_at = NOW() - INTERVAL 1 DAY WHERE id = %s",
                    (job.id,))
    conn.commit()

    _consumer(int_db_config, int_consumer_config)._recover_stale_jobs()

    assert recorder.outcomes == [("failed", True)]
    assert fetch_job(job.id)["status"] == "DEAD"


# --- with nothing registered, today's behaviour ------------------------------

@pytest.mark.parametrize("status,error,expected", [
    ("RUNNING", None, "SUCCESS"),
    ("RUNNING", Boom("x"), "TODO"),
])
def test_with_no_finalizer_the_transition_is_unchanged(
    conn, int_db_config, int_consumer_config, status, error, expected
):
    clear_hooks()
    job = _insert(conn, status=status)

    _consumer(int_db_config, int_consumer_config)._finalize_job(
        job, error=error, job_result=_JobResult(summary="ok"), elapsed_ms=10)

    assert fetch_job(job.id)["status"] == expected


# --- job.failed is the framework's own row -----------------------------------

def test_a_failing_job_writes_job_failed_with_no_finalizer_registered(
    conn, int_db_config, int_consumer_config
):
    """The module-disabled durability guarantee: the row a waiting reader most needs."""
    clear_hooks()
    job = _insert(conn, attempt=3, max_attempts=3)
    job.execution_id = None

    _consumer(int_db_config, int_consumer_config)._finalize_job(
        job, error=Boom("fatal"), job_result=None, elapsed_ms=10)

    rows = _outbox(conn)
    assert [r["kind"] for r in rows] == ["job.failed"]
    assert json.loads(rows[0]["payload"]) == {
        "job_id": job.id, "attempt": 3, "kind": "internal", "execution_id": None}
    assert rows[0]["execution_id"] is None


def test_job_failed_carries_the_execution_id_when_one_was_minted(
    conn, int_db_config, int_consumer_config
):
    clear_hooks()
    job = _insert(conn, attempt=3, max_attempts=3)

    _consumer(int_db_config, int_consumer_config)._finalize_job(
        job, error=Boom("fatal"), job_result=None, elapsed_ms=10)

    rows = _outbox(conn)
    assert rows[0]["execution_id"] == "exec-under-test"
    assert json.loads(rows[0]["payload"])["execution_id"] == "exec-under-test"


def test_job_failed_carries_no_exception_text_and_no_agent_output(
    conn, int_db_config, int_consumer_config
):
    clear_hooks()
    job = _insert(conn, attempt=3, max_attempts=3)
    error = Boom("the database password is hunter2")
    error.agent_output = "the agent said something quotable"     # type: ignore[attr-defined]

    _consumer(int_db_config, int_consumer_config)._finalize_job(
        job, error=error, job_result=None, elapsed_ms=10)

    payload = _outbox(conn)[0]["payload"]
    assert "hunter2" not in payload and "quotable" not in payload
    assert set(json.loads(payload)) == {"job_id", "attempt", "kind", "execution_id"}


def test_a_retry_also_writes_job_failed(conn, int_db_config, int_consumer_config):
    clear_hooks()
    job = _insert(conn, attempt=1, max_attempts=3)

    _consumer(int_db_config, int_consumer_config)._finalize_job(
        job, error=Boom("transient"), job_result=None, elapsed_ms=10)

    assert [r["kind"] for r in _outbox(conn)] == ["job.failed"]


def test_a_pool_wait_writes_no_job_failed(conn, int_db_config, int_consumer_config):
    """Waiting for a credential pool is not a failure - it is why the attempt is refunded."""
    from datetime import UTC, datetime, timedelta

    from agento.framework.agent_manager.errors import CredentialsBusyError

    clear_hooks()
    job = _insert(conn)

    _consumer(int_db_config, int_consumer_config)._finalize_job(
        job,
        error=CredentialsBusyError(
            "busy", pool_retry_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=5)),
        job_result=None, elapsed_ms=10)

    assert _outbox(conn) == []


def test_a_successful_job_writes_no_job_failed(conn, int_db_config, int_consumer_config):
    clear_hooks()
    job = _insert(conn)

    _consumer(int_db_config, int_consumer_config)._finalize_job(
        job, error=None, job_result=_JobResult(summary="ok"), elapsed_ms=10)

    assert _outbox(conn) == []


@pytest.mark.parametrize("error_class,expected", [
    ("TimeoutError", "timeout"),
    ("SubprocessTimeoutError", "timeout"),
    ("AuthenticationError", "credential_error"),
    ("UsageLimitError", "credential_error"),
    ("CredentialsBusyError", "credential_error"),
    ("CalledProcessError", "harness_error"),
    ("JobVerificationFailed", "harness_error"),
    ("ValueError", "internal"),
    (None, "internal"),
])
def test_the_failure_kind_is_a_closed_vocabulary(error_class, expected):
    """Four kinds, because a reader decides whether to wait, re-ask or page a human. A
    Python class name in a durable payload would be an internal identifier in a public
    contract."""
    assert failure_kind(error_class) == expected


# --- EVT-4: a run's start and finish fire together or not at all -----------

def _execution_events() -> list[tuple[str, object]]:
    """Every execution event dispatched, in order."""
    from agento.framework.event_manager import ObserverEntry, clear, get_event_manager

    clear()
    seen: list[tuple[str, object]] = []
    for name in ("execution_start_after", "execution_abandon_after",
                 "execution_finish_after"):
        get_event_manager().register(name, ObserverEntry(
            name=f"spy_{name}",
            observer_class=type("Spy", (), {
                "execute": (lambda n: lambda self, event: seen.append((n, event)))(name)}),
        ))
    return seen


@pytest.mark.parametrize("execution_id", ["e-abort", None])
def test_the_capability_mint_abort_announces_the_start_it_abandons(
    conn, recorder, int_db_config, int_consumer_config, int_agent_view, execution_id
):
    """This process minted the execution row, so this attempt really did start.

    Announcing only the abandon would hand an observer a finish for a run it never saw
    begin (EVT-4). The stale-recovery paths are deliberately NOT paired here: they
    finalize another process's run, whose start fired in that process, and their
    `execution_id=None` says so.
    """
    from agento.framework.event_manager import clear

    seen = _execution_events()
    try:
        job = _insert(conn, status="PAUSED")
        job.agent_view_id = int_agent_view
        # `None` is the no-provider case: nothing minted an execution id, and
        # `_end_execution` announces the abandon with that same None. Pairing only the
        # runs that HAVE an id would leave exactly this one unpaired again.
        job.execution_id = execution_id

        assert _consumer(int_db_config, int_consumer_config)._issue_run_capabilities(
            conn, job) is None

        names = [n for n, _ in seen]
        assert names[0] == "execution_start_after"
        assert "execution_abandon_after" in names
        assert seen[0][1].execution_id == execution_id
    finally:
        clear()
