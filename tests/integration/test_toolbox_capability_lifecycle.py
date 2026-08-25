"""Integration: a run's toolbox capabilities across the whole job lifecycle (real MySQL).

These cases exist on the REAL database on purpose. The unit suite
(`tests/unit/framework/test_consumer_capability.py`) drives the same code with a fake
connection, which can assert the SQL that was issued but not what the database DOES with
it: that a revoke shares the terminal status's transaction, that a failing revoke takes the
status change back with it, and — for the pause race — that `SELECT ... FOR UPDATE` really
serialises two connections. A fake connection cannot exhibit a race between two sessions.
"""
from __future__ import annotations

import logging
import threading
import time
from unittest.mock import patch

import pytest

from agento.framework.consumer import Consumer
from agento.framework.db import get_connection
from agento.framework.job_models import Job
from agento.framework.job_store import pause_job
from agento.framework.toolbox_capability import (
    KIND_INTERNAL_REST,
    KIND_MCP_JOB,
    MCP_CAPABILITY_TTL_SECONDS,
    issue_capability,
)
from agento.modules.claude.src.output_parser import ClaudeResult
from agento.modules.claude.src.runner import ClaudeSubprocessRunner

from .conftest import (
    _test_connection,
    fetch_job,
    insert_primary_token,
    insert_queued_job,
    update_job,
)

logger = logging.getLogger("test")


def capabilities_for(job_id: int) -> list[dict]:
    """Every capability row ever written for a job, revoked or not."""
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT kind, agent_view_id, job_id, revoked_at FROM toolbox_capability "
                "WHERE job_id = %s ORDER BY id",
                (job_id,),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def _success() -> ClaudeResult:
    return ClaudeResult(
        raw_output="ok", input_tokens=10, output_tokens=5, cost_usd=0.01,
        num_turns=1, duration_ms=100, session_id="s1",
    )


def _run_one_job(int_db_config, int_consumer_config, *, execute):
    """Claim and execute exactly one job, returning the Job the consumer claimed."""
    with patch.object(ClaudeSubprocessRunner, "execute", **execute):
        consumer = Consumer(int_db_config, int_consumer_config, logger)
        job = consumer._try_dequeue()
        assert job is not None
        consumer._execute_job(job)
        return job


class TestTerminalTransitionsRevoke:
    """Every terminal branch must retire the run's capabilities in the SAME transaction."""

    def test_capability_is_revoked_on_success(
        self, int_db_config, int_consumer_config, int_agent_view
    ):
        insert_primary_token("claude")
        job_id = insert_queued_job(
            reference_id="AI-1", idempotency_key="cap:success", agent_view_id=int_agent_view
        )

        _run_one_job(int_db_config, int_consumer_config, execute={"return_value": _success()})

        assert fetch_job(job_id)["status"] == "SUCCESS"
        rows = capabilities_for(job_id)
        assert rows, "the run minted no capability at all"
        assert all(r["revoked_at"] is not None for r in rows)

    def test_capability_is_revoked_on_retry_to_todo(
        self, int_db_config, int_consumer_config, int_agent_view
    ):
        insert_primary_token("claude")
        job_id = insert_queued_job(
            reference_id="AI-2", idempotency_key="cap:retry", agent_view_id=int_agent_view
        )

        _run_one_job(
            int_db_config, int_consumer_config,
            execute={"side_effect": RuntimeError("transient")},
        )

        # Back to TODO for another attempt — and the previous attempt's token is dead, so a
        # copy taken out of the workspace cannot be replayed while the job waits to re-run.
        assert fetch_job(job_id)["status"] == "TODO"
        rows = capabilities_for(job_id)
        assert rows
        assert all(r["revoked_at"] is not None for r in rows)

    def test_capability_is_revoked_on_dead(
        self, int_db_config, int_consumer_config, int_agent_view
    ):
        insert_primary_token("claude")
        job_id = insert_queued_job(
            reference_id="AI-3", idempotency_key="cap:dead", agent_view_id=int_agent_view
        )

        _run_one_job(
            int_db_config, int_consumer_config,
            execute={"side_effect": ValueError("permanent")},
        )

        assert fetch_job(job_id)["status"] == "DEAD"
        rows = capabilities_for(job_id)
        assert rows
        assert all(r["revoked_at"] is not None for r in rows)

    def test_a_discovery_job_gets_both_kinds_and_both_are_revoked(
        self, int_db_config, int_consumer_config, int_agent_view
    ):
        """reference_id IS NULL is the discovery flow — it also needs the REST kind."""
        insert_primary_token("claude")
        job_id = insert_queued_job(
            reference_id=None, idempotency_key="cap:discovery",
            agent_view_id=int_agent_view,
        )

        _run_one_job(int_db_config, int_consumer_config, execute={"return_value": _success()})

        rows = capabilities_for(job_id)
        assert {r["kind"] for r in rows} == {KIND_MCP_JOB, KIND_INTERNAL_REST}
        assert all(r["revoked_at"] is not None for r in rows)

    def test_a_blank_job_gets_no_capability(
        self, int_db_config, int_consumer_config
    ):
        """agent_view_id IS NULL: nothing to scope a capability to, so none is written."""
        insert_primary_token("claude")
        job_id = insert_queued_job(
            reference_id="AI-4", idempotency_key="cap:blank", agent_view_id=None
        )

        _run_one_job(int_db_config, int_consumer_config, execute={"return_value": _success()})

        assert capabilities_for(job_id) == []


class TestStaleRecoveryRevokes:

    @pytest.mark.parametrize(
        ("attempt", "max_attempts", "expected"),
        [(0, 3, "TODO"), (3, 3, "DEAD")],
    )
    def test_capability_is_revoked_when_a_stale_job_is_recovered(
        self, int_db_config, int_consumer_config, int_agent_view,
        attempt, max_attempts, expected,
    ):
        """Both recovery branches revoke — a dead process must not leave a live token."""
        job_id = insert_queued_job(
            reference_id="AI-5", idempotency_key=f"cap:stale:{expected}",
            max_attempts=max_attempts, agent_view_id=int_agent_view,
        )
        # A RUNNING job whose PID is long gone. 2**22 is above the default pid_max, so it
        # names no live process without the test having to kill one.
        update_job(job_id, status="RUNNING", pid=2**22, attempt=attempt)

        conn = get_connection(int_db_config)
        try:
            issue_capability(
                conn, kind=KIND_MCP_JOB, agent_view_id=int_agent_view,
                job_id=job_id, ttl_seconds=MCP_CAPABILITY_TTL_SECONDS,
            )
        finally:
            conn.close()

        Consumer(int_db_config, int_consumer_config, logger)._recover_stale_jobs()

        assert fetch_job(job_id)["status"] == expected
        rows = capabilities_for(job_id)
        assert rows
        assert all(r["revoked_at"] is not None for r in rows)


class TestRevokeFailureRollsBack:

    def test_a_failed_revoke_rolls_the_status_change_back(
        self, int_db_config, int_consumer_config, int_agent_view
    ):
        """The revoke shares the terminal status's transaction, so it cannot be half-applied.

        A job left SUCCESS with a live capability would be exactly the leak this design
        prevents; leaving it RUNNING hands it to `_recover_stale_jobs`, which revokes too.
        """
        insert_primary_token("claude")
        job_id = insert_queued_job(
            reference_id="AI-6", idempotency_key="cap:revokefail", agent_view_id=int_agent_view
        )

        boom = RuntimeError("revoke exploded")
        with patch(
            "agento.framework.consumer.revoke_job_capabilities", side_effect=boom
        ):
            _run_one_job(
                int_db_config, int_consumer_config, execute={"return_value": _success()}
            )

        row = fetch_job(job_id)
        assert row["status"] == "RUNNING", "the terminal status survived a failed revoke"


class TestPauseRace:
    """`_issue_run_capabilities` takes `SELECT ... FOR UPDATE` on the job row.

    Without it a pause landing between the claim and the mint revokes nothing (there is
    nothing yet), the consumer then commits capabilities for an already-PAUSED job, and
    `_finalize_job` skips a paused job — so the tokens live until they expire.
    """

    def _running_job(self, int_agent_view, key: str) -> int:
        job_id = insert_queued_job(
            reference_id="AI-7", idempotency_key=key, agent_view_id=int_agent_view
        )
        update_job(job_id, status="RUNNING", pid=None)
        return job_id

    def test_pause_between_claim_and_mint_issues_no_capability(
        self, int_db_config, int_consumer_config, int_agent_view
    ):
        job_id = self._running_job(int_agent_view, "cap:race:pause-first")

        pause_conn = get_connection(int_db_config)
        try:
            pause_job(pause_conn, job_id)
        finally:
            pause_conn.close()

        job = Job.from_row(fetch_job(job_id))
        consumer = Consumer(int_db_config, int_consumer_config, logger)
        conn = get_connection(int_db_config)
        try:
            assert consumer._issue_run_capabilities(conn, job) is None
        finally:
            conn.close()

        assert fetch_job(job_id)["status"] == "PAUSED"
        assert capabilities_for(job_id) == []

    def test_pause_after_mint_revokes_both_kinds(
        self, int_db_config, int_consumer_config, int_agent_view
    ):
        """Mint commits first: the pause's revoke-by-job_id then finds and retires the rows.

        The ordering is forced, not raced: the pause runs on its own connection and must
        BLOCK while the mint's transaction holds the job row, then find and retire both
        rows once it commits. What the block proves is the guarantee that matters — a pause
        cannot complete beside an in-flight mint and revoke nothing. It does NOT isolate
        which statement takes the lock (`pause_job`'s `FOR UPDATE` read or its own
        status-guarded `UPDATE`); the pause-first case above is what pins the consumer's
        side of the same serialisation.
        """
        job_id = self._running_job(int_agent_view, "cap:race:mint-first")
        job = Job.from_row(fetch_job(job_id))
        # Discovery flow (no reference_id) so BOTH kinds are minted and must be revoked.
        job.reference_id = None

        entered = threading.Event()
        outcome: list[BaseException | None] = []

        def pause_in_parallel():
            conn = get_connection(int_db_config)
            try:
                entered.set()
                pause_job(conn, job_id)
                outcome.append(None)
            except BaseException as exc:  # asserted below, never swallowed
                outcome.append(exc)
            finally:
                conn.close()

        consumer = Consumer(int_db_config, int_consumer_config, logger)
        conn = get_connection(int_db_config)
        thread = threading.Thread(target=pause_in_parallel)
        try:
            # Take the row lock the real issuance path takes, and HOLD it.
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM job WHERE id = %s FOR UPDATE", (job.id,))
                assert cur.fetchone()["status"] == "RUNNING"

            thread.start()
            assert entered.wait(timeout=10)
            time.sleep(1.0)
            assert thread.is_alive(), (
                "pause_job completed while the job row was locked — a pause that does not "
                "wait for an in-flight mint revokes nothing and the tokens outlive it"
            )
            assert not outcome, "pause_job returned without waiting for the lock"

            # The real issuance path: it retakes the lock this connection already holds,
            # writes both kinds, and COMMITS — which is what releases the pause.
            assert consumer._issue_run_capabilities(conn, job) is not None
        finally:
            conn.close()
            thread.join(timeout=30)

        assert not thread.is_alive(), "pause_job never got the row lock"
        assert outcome == [None], f"pause_job failed: {outcome}"

        assert fetch_job(job_id)["status"] == "PAUSED"
        rows = capabilities_for(job_id)
        assert {r["kind"] for r in rows} == {KIND_MCP_JOB, KIND_INTERNAL_REST}
        assert all(r["revoked_at"] is not None for r in rows), (
            "a paused job kept a live capability"
        )
