from __future__ import annotations

import logging
import signal
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.consumer import Consumer, _JobResult
from agento.framework.harness import RunResult
from agento.framework.job_models import AgentType, Job, JobStatus
from agento.modules.claude.src.output_parser import ClaudeResult

pytestmark = pytest.mark.usefixtures("builtin_harnesses")


def _mock_resolved_token():
    """Create a mock token returned by CredentialResolver.resolve."""
    token = MagicMock()
    token.id = 1
    token.credentials = {"subscription_key": "sk-test"}
    token.scope = "claude"
    return token


def _make_job(**overrides) -> Job:
    defaults = dict(
        id=1,
        schedule_id=None,
        type=AgentType.CRON,
        source="jira",
        agent_view_id=None,
        priority=50,
        reference_id="AI-1",
        agent_type=None,
        provider=None,
        model=None,
        input_tokens=None,
        output_tokens=None,
        prompt=None,
        output=None,
        context=None,
        idempotency_key="jira:cron:AI-1:20260220_0800",
        status=JobStatus.TODO,
        attempt=0,
        max_attempts=3,
        scheduled_after=datetime(2026, 2, 20, 8, 0),
        started_at=None,
        finished_at=None,
        result_summary=None,
        error_message=None,
        error_class=None,
        pid=None,
        session_id=None,
        created_at=datetime(2026, 2, 20, 7, 59),
        updated_at=datetime(2026, 2, 20, 7, 59),
    )
    defaults.update(overrides)
    return Job(**defaults)



def _statements(cursor):
    """Every statement the code under test ran, minus the one the POOL runs.

    `Consumer._db()` borrows with `isolation="READ COMMITTED"`, so every checkout opens
    with a `SET SESSION TRANSACTION ISOLATION LEVEL` and closes by restoring
    `transaction_isolation`. Both are the pool's statements, not the caller's, and a test
    that counts or indexes `execute` calls must not see them.
    """
    return [c for c in cursor.execute.call_args_list
            if "ISOLATION" not in c[0][0].upper()
            and "transaction_isolation" not in c[0][0]]


def _fails_except_pool(exc):
    """Make every statement raise EXCEPT the pool's own isolation statements.

    A connection too dead to take `SET SESSION TRANSACTION ISOLATION LEVEL` is discarded
    at checkout and never reaches the caller, so a test about the CALLER's error handling
    must let that one statement through.
    """
    def execute(sql, *args):
        if "ISOLATION" in sql.upper() or "transaction_isolation" in sql:
            return None
        raise exc
    return execute


def _mock_connection(row=None):
    """Create mock connection with optional fetchone result."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = row
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cursor


def _last_job_update(mock_cursor):
    """The last `UPDATE job ...` execute call.

    Finalize also revokes the job's toolbox capabilities in the same transaction,
    so the LAST execute is no longer the status write.
    """
    for call in reversed(_statements(mock_cursor)):
        if "UPDATE job" in call[0][0]:
            return call[0]
    raise AssertionError("no UPDATE job executed")


def _make_row(**overrides) -> dict:
    row = {
        "id": 1,
        "schedule_id": None,
        "type": "cron",
        "source": "jira",
        "agent_view_id": None,
        "priority": 50,
        "reference_id": "AI-1",
        "agent_type": None,
        "model": None,
        "input_tokens": None,
        "output_tokens": None,
        "prompt": None,
        "output": None,
        "context": None,
        "idempotency_key": "jira:cron:AI-1:20260220_0800",
        "status": "TODO",
        "attempt": 0,
        "max_attempts": 3,
        "scheduled_after": datetime(2026, 2, 20, 8, 0),
        "started_at": None,
        "finished_at": None,
        "result_summary": None,
        "error_message": None,
        "error_class": None,
        "pid": None,
        "session_id": None,
        "created_at": datetime(2026, 2, 20, 7, 59),
        "updated_at": datetime(2026, 2, 20, 7, 59),
    }
    row.update(overrides)
    return row


def _make_claude_result(**overrides) -> ClaudeResult:
    defaults = dict(
        raw_output="ok",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.01,
        num_turns=3,
        duration_ms=5000,
        session_id="success",
        harness="claude",
        prompt=None,
    )
    defaults.update(overrides)
    return ClaudeResult(**defaults)


# ---- Section 4: Stale job recovery ----


class TestRecoverStaleJobs:
    # Dead owner -> TODO/DEAD is pinned in test_consumer_events.test_stale_recovery_branch_pins.
    @pytest.mark.parametrize(("state", "started_at"), [("alive", None), ("unknown", datetime(2020, 1, 1))])
    @patch("agento.framework.consumer.get_connection")
    def test_a_run_its_owner_does_not_call_dead_is_left_alone(
        self, mock_get_conn, state, started_at, sample_db_config, sample_consumer_config,
    ):
        """An owner that does not answer is not a dead run: no timeout turns unknown into
        dead, even for a job that started long ago (WS5 owner rule)."""
        mock_conn, mock_cursor = _mock_connection()
        mock_cursor.fetchall.return_value = [
            {"id": 1, "reference_id": "AI-1", "pid": 7, "runner_ref": "runner-1.sock:b1",
             "attempt": 1, "max_attempts": 3, "started_at": started_at},
        ]
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        with patch("agento.framework.consumer.runner_client.alive", return_value=state) as alive:
            consumer._recover_stale_jobs()

        assert len(_statements(mock_cursor)) == 1  # only the SELECT
        mock_conn.commit.assert_called_once()
        alive.assert_called_once_with("runner-1.sock:b1", "job:1")

    @patch("agento.framework.consumer.get_connection")
    def test_a_pid_from_before_the_runner_is_dead(self, mock_get_conn, sample_db_config, sample_consumer_config):
        """No owner on record but a pid: the run was in the old cron container, which the
        upgrade replaced. Recovered at once, and no runner is asked."""
        mock_conn, mock_cursor = _mock_connection()
        mock_cursor.fetchall.return_value = [
            {"id": 1, "reference_id": "AI-1", "pid": 4242, "runner_ref": None,
             "attempt": 1, "max_attempts": 3, "started_at": datetime.now()},
        ]
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        with patch("agento.framework.consumer.runner_client.alive") as alive:
            consumer._recover_stale_jobs()

        alive.assert_not_called()
        assert "status = 'TODO'" in _statements(mock_cursor)[1][0][0]

    @patch("agento.framework.consumer.get_connection")
    def test_recover_null_pid_old_job_treated_as_dead(self, mock_get_conn, sample_config, sample_db_config, sample_consumer_config):
        """RUNNING job with NULL pid and old started_at -> treated as dead (timestamp fallback)."""
        mock_conn, mock_cursor = _mock_connection()
        # started_at far in the past — exceeds threshold
        old_started = datetime(2020, 1, 1, 0, 0, 0)
        mock_cursor.fetchall.return_value = [
            {"id": 1, "reference_id": "AI-1", "pid": None, "attempt": 1, "max_attempts": 3, "started_at": old_started},
        ]
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        consumer._recover_stale_jobs()

        # SELECT + UPDATE + capability revoke (null PID, old timestamp)
        assert len(_statements(mock_cursor)) == 3
        update_sql = _statements(mock_cursor)[1][0][0]
        assert "status = 'TODO'" in update_sql

    @patch("agento.framework.consumer.get_connection")
    def test_recover_null_pid_fresh_job_skipped(self, mock_get_conn, sample_config, sample_db_config, sample_consumer_config):
        """RUNNING job with NULL pid but recent started_at -> skipped (PID callback hasn't fired yet)."""
        mock_conn, mock_cursor = _mock_connection()
        # started_at is now — well within threshold
        mock_cursor.fetchall.return_value = [
            {"id": 1, "reference_id": "AI-1", "pid": None, "attempt": 1, "max_attempts": 3, "started_at": datetime.now()},
        ]
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        consumer._recover_stale_jobs()

        # Only SELECT, no UPDATE — fresh job without PID is not touched
        assert len(_statements(mock_cursor)) == 1
        mock_conn.commit.assert_called_once()

    @patch("agento.framework.consumer.get_connection")
    def test_recover_db_error_does_not_crash(self, mock_get_conn, sample_config, sample_db_config, sample_consumer_config):
        mock_get_conn.side_effect = RuntimeError("DB down")

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))

        # Should not raise
        consumer._recover_stale_jobs()

    @patch("agento.framework.consumer.get_connection")
    def test_recover_skips_paused_jobs(self, mock_get_conn, sample_config, sample_db_config, sample_consumer_config):
        """PAUSED jobs are not returned by the recovery query (status = 'RUNNING' filter)."""
        mock_conn, mock_cursor = _mock_connection()
        # Recovery query returns only RUNNING jobs — PAUSED jobs won't appear
        mock_cursor.fetchall.return_value = []
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        consumer._recover_stale_jobs()

        # Only the SELECT, no UPDATEs
        assert len(_statements(mock_cursor)) == 1
        sql = _statements(mock_cursor)[0][0][0]
        assert "status = 'RUNNING'" in sql


# ---- Section 5: Dequeue ----




class TestDequeue:
    @patch("agento.framework.consumer.get_connection")
    def test_dequeue_empty_queue(self, mock_get_conn, sample_config, sample_db_config, sample_consumer_config):
        mock_conn, _mock_cursor = _mock_connection(row=None)
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        result = consumer._try_dequeue()

        assert result is None
        mock_conn.rollback.assert_called()
        mock_conn.commit.assert_not_called()

    @patch("agento.framework.consumer.get_connection")
    def test_dequeue_claims_job(self, mock_get_conn, sample_config, sample_db_config, sample_consumer_config):
        row = _make_row()
        mock_conn, _mock_cursor = _mock_connection(row=row)
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = consumer._try_dequeue()

        assert job is not None
        assert job.status == JobStatus.RUNNING
        mock_conn.commit.assert_called_once()

    @patch("agento.framework.consumer.get_connection")
    def test_dequeue_increments_attempt(self, mock_get_conn, sample_config, sample_db_config, sample_consumer_config):
        row = _make_row(attempt=2)
        mock_conn, _mock_cursor = _mock_connection(row=row)
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = consumer._try_dequeue()

        assert job is not None
        assert job.attempt == 3  # 2 + 1

    @patch("agento.framework.consumer.get_connection")
    def test_dequeue_error_returns_none(self, mock_get_conn, sample_config, sample_db_config, sample_consumer_config):
        mock_conn, mock_cursor = _mock_connection()
        # Not on the pool's `SET TRANSACTION`: a connection too dead for that never
        # reaches the caller's try block, and this test is about the claim failing.
        mock_cursor.execute.side_effect = _fails_except_pool(RuntimeError("DB error"))
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        result = consumer._try_dequeue()

        assert result is None
        mock_conn.rollback.assert_called()
        mock_conn.commit.assert_not_called()

    @patch("agento.framework.consumer.get_connection")
    def test_dequeue_gives_its_connection_back_to_the_pool(self, mock_get_conn, sample_config, sample_db_config, sample_consumer_config):
        mock_conn, _mock_cursor = _mock_connection(row=_make_row())
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        consumer._try_dequeue()
        consumer._try_dequeue()

        assert mock_get_conn.call_count == 1  # the second claim reused the first connection
        mock_conn.close.assert_not_called()


# ---- Section 6: Execution dispatch ----


class TestRunJob:
    @pytest.fixture(autouse=True)
    def _mock_runtime(self):
        from agento.framework.agent_view_runtime import AgentViewRuntime
        runtime = AgentViewRuntime()
        runtime.harness = "claude"
        runtime.provider = "anthropic"
        with patch("agento.framework.consumer.resolve_agent_view_runtime",
                   return_value=runtime):
            yield

    @pytest.fixture(autouse=True)
    def _mock_token_resolver(self):
        with patch("agento.framework.consumer.CredentialResolver") as MockCls:
            mock_resolver = MagicMock()
            mock_resolver.resolve.return_value = _mock_resolved_token()
            MockCls.return_value = mock_resolver
            self._token_resolver_mock = mock_resolver
            yield

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.RemoteRunner")
    @patch("agento.framework.consumer.get_connection")
    def test_run_job_cron(self, mock_conn, MockRunner, mock_get_ch, mock_get_wf, sample_config, sample_db_config, sample_consumer_config):
        mock_conn.return_value = MagicMock()
        mock_result = _make_claude_result()
        mock_workflow = MagicMock()
        mock_workflow.execute_job.return_value = mock_result
        mock_get_wf.return_value.return_value = mock_workflow

        mock_channel = MagicMock()
        mock_channel.name = "jira"
        mock_get_ch.return_value = mock_channel

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(type=AgentType.CRON, reference_id="AI-1")

        result = consumer._run_job(job)

        mock_get_ch.assert_called_once_with("jira")
        mock_get_wf.assert_called_once_with(AgentType.CRON)
        mock_workflow.execute_job.assert_called_once()
        assert mock_workflow.execute_job.call_args[0][:2] == (mock_channel, job)
        # create_runner(harness, ctx, ...) — everything per-run now travels in the
        # context, and the CALLER has already claimed the credential.
        MockRunner.assert_called_once()
        harness, ctx = MockRunner.call_args.args
        assert harness == "claude"
        assert MockRunner.call_args.kwargs == {
            "logger": consumer.logger, "dry_run": False,
        }
        assert ctx.harness == "claude"
        assert ctx.provider == "anthropic"
        assert ctx.timeout_seconds == sample_consumer_config.job_timeout_seconds
        assert ctx.model is None
        assert ctx.home_dir is None
        assert ctx.credential is self._token_resolver_mock.resolve.return_value
        assert ctx.extra_env == {}  # no agent_view ⇒ no git identity env
        assert isinstance(result, _JobResult)
        assert "session_id=" in result.summary

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.RemoteRunner")
    @patch("agento.framework.consumer.get_connection")
    def test_run_job_cron_no_reference_id_raises(self, mock_conn, MockRunner, mock_get_ch, mock_get_wf, sample_config, sample_db_config, sample_consumer_config):
        mock_conn.return_value = MagicMock()
        mock_get_ch.return_value = MagicMock(name="jira")
        mock_workflow = MagicMock()
        mock_workflow.execute_job.side_effect = ValueError("Cron job 1 has no reference_id")
        mock_get_wf.return_value.return_value = mock_workflow

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(type=AgentType.CRON, reference_id=None)

        with pytest.raises(ValueError, match="no reference_id"):
            consumer._run_job(job)

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.RemoteRunner")
    @patch("agento.framework.consumer.get_connection")
    def test_run_job_todo_specific(self, mock_conn, MockRunner, mock_get_ch, mock_get_wf, sample_config, sample_db_config, sample_consumer_config):
        mock_conn.return_value = MagicMock()
        mock_result = _make_claude_result()
        mock_workflow = MagicMock()
        mock_workflow.execute_job.return_value = mock_result
        mock_get_wf.return_value.return_value = mock_workflow

        mock_channel = MagicMock()
        mock_channel.name = "jira"
        mock_get_ch.return_value = mock_channel

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(type=AgentType.TODO, reference_id="AI-2")

        result = consumer._run_job(job)

        mock_workflow.execute_job.assert_called_once()
        assert mock_workflow.execute_job.call_args[0][:2] == (mock_channel, job)
        assert isinstance(result, _JobResult)
        assert "session_id=" in result.summary

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.RemoteRunner")
    @patch("agento.framework.consumer.get_connection")
    def test_run_job_todo_no_ref_delegates_to_workflow(
        self, mock_conn, MockRunner, mock_get_ch, mock_get_wf,
        sample_config, sample_db_config, sample_consumer_config,
    ):
        """Consumer passes through to workflow -- no TODO-specific branching."""
        mock_conn.return_value = MagicMock()
        no_work_result = RunResult(raw_output="No TODO tasks found", session_id="no_work")
        mock_workflow = MagicMock()
        mock_workflow.execute_job.return_value = no_work_result
        mock_get_wf.return_value.return_value = mock_workflow

        mock_channel = MagicMock()
        mock_channel.name = "jira"
        mock_get_ch.return_value = mock_channel

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(type=AgentType.TODO, reference_id=None)

        result = consumer._run_job(job)

        mock_workflow.execute_job.assert_called_once()
        assert isinstance(result, _JobResult)
        assert result.summary == "No TODO tasks found"

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.RemoteRunner")
    @patch("agento.framework.consumer.get_connection")
    def test_run_job_followup(self, mock_conn, MockRunner, mock_get_ch, mock_get_wf, sample_config, sample_db_config, sample_consumer_config):
        mock_conn.return_value = MagicMock()
        mock_result = _make_claude_result()
        mock_workflow = MagicMock()
        mock_workflow.execute_job.return_value = mock_result
        mock_get_wf.return_value.return_value = mock_workflow

        mock_channel = MagicMock()
        mock_channel.name = "jira"
        mock_get_ch.return_value = mock_channel

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(
            type=AgentType.FOLLOWUP,
            reference_id="AI-3",
            context="Check reindex status",
            source="jira",
        )

        result = consumer._run_job(job)

        mock_workflow.execute_job.assert_called_once()
        assert mock_workflow.execute_job.call_args[0][:2] == (mock_channel, job)
        assert isinstance(result, _JobResult)
        assert "session_id=" in result.summary

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.RemoteRunner")
    @patch("agento.framework.consumer.get_connection")
    def test_run_job_followup_no_reference_id_raises(self, mock_conn, MockRunner, mock_get_ch, mock_get_wf, sample_config, sample_db_config, sample_consumer_config):
        mock_conn.return_value = MagicMock()
        mock_get_ch.return_value = MagicMock(name="jira")
        mock_workflow = MagicMock()
        mock_workflow.execute_job.side_effect = ValueError("Followup job 1 has no reference_id")
        mock_get_wf.return_value.return_value = mock_workflow

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(
            type=AgentType.FOLLOWUP,
            reference_id=None,
            context="some instructions",
        )

        with pytest.raises(ValueError, match="no reference_id"):
            consumer._run_job(job)

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.RemoteRunner")
    @patch("agento.framework.consumer.get_connection")
    def test_run_job_followup_no_context_raises(self, mock_conn, MockRunner, mock_get_ch, mock_get_wf, sample_config, sample_db_config, sample_consumer_config):
        mock_conn.return_value = MagicMock()
        mock_get_ch.return_value = MagicMock(name="jira")
        mock_workflow = MagicMock()
        mock_workflow.execute_job.side_effect = ValueError("Followup job 1 has no context")
        mock_get_wf.return_value.return_value = mock_workflow

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(
            type=AgentType.FOLLOWUP,
            reference_id="AI-3",
            context=None,
        )

        with pytest.raises(ValueError, match="no context"):
            consumer._run_job(job)

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.RemoteRunner")
    @patch("agento.framework.consumer.get_connection")
    def test_run_job_raises_when_harness_unset(self, mock_conn, MockRunner, mock_get_ch, mock_get_wf, sample_config, sample_db_config, sample_consumer_config):
        """When no agent_view/provider is configured, the consumer raises with an actionable message."""
        from agento.framework.agent_view_runtime import AgentViewRuntime

        mock_conn.return_value = MagicMock()
        runtime = AgentViewRuntime()
        runtime.provider = None

        with patch(
            "agento.framework.consumer.resolve_agent_view_runtime",
            return_value=runtime,
        ):
            consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
            job = _make_job(type=AgentType.CRON, reference_id="AI-1")

            with pytest.raises(RuntimeError, match="No agent_view/harness configured"):
                consumer._run_job(job)

        self._token_resolver_mock.resolve.assert_not_called()

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.RemoteRunner")
    @patch("agento.framework.consumer.get_connection")
    def test_run_job_resumes_when_session_id_present(
        self, mock_conn, MockRunner, mock_get_ch, mock_get_wf,
        sample_config, sample_db_config, sample_consumer_config,
    ):
        """attempt > 1 with a session_id ⇒ one execute() carrying that session_id.

        There is no separate ``resume()`` entry point any more — resuming is just a
        RunRequest with ``session_id`` set, which is what the CommandBuilder turns into
        the harness's own resume flags.
        """
        mock_conn.return_value = MagicMock()

        resume_result = _make_claude_result(session_id="sess-resume")
        mock_runner = MagicMock()
        mock_runner.execute.return_value = resume_result
        MockRunner.return_value = mock_runner

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(
            type=AgentType.CRON,
            reference_id="AI-1",
            attempt=2,
            session_id="sess-abc",
            pid=99999,
            runner_ref="runner-1.sock:b1",
            status=JobStatus.RUNNING,
        )

        with patch("agento.framework.consumer.runner_client.alive", return_value="dead"):
            result = consumer._run_job(job)

        mock_runner.execute.assert_called_once()
        request = mock_runner.execute.call_args.args[0]
        assert request.session_id == "sess-abc"
        assert request.model is None
        assert "resumed" in result.summary
        assert result.session_id == "sess-resume"

    @pytest.mark.parametrize("state", ["alive", "unknown"])
    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.RemoteRunner")
    @patch("agento.framework.consumer.get_connection")
    def test_a_previous_run_that_may_still_go_on_is_never_resumed(
        self, mock_conn, MockRunner, mock_get_ch, mock_get_wf, state,
        sample_config, sample_db_config, sample_consumer_config,
    ):
        """The owner says the old run is alive, or cannot be asked: a fresh run, so one
        session is never driven twice (WS5 owner rule)."""
        mock_conn.return_value = MagicMock()
        mock_workflow = MagicMock()
        mock_workflow.execute_job.return_value = _make_claude_result()
        mock_get_wf.return_value.return_value = mock_workflow
        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(type=AgentType.CRON, reference_id="AI-1", attempt=2, session_id="sess-abc",
                        pid=7, runner_ref="runner-1.sock:b1", status=JobStatus.RUNNING)

        with patch("agento.framework.consumer.runner_client.alive", return_value=state):
            consumer._run_job(job)

        MockRunner.return_value.execute.assert_not_called()
        mock_workflow.execute_job.assert_called_once()

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.RemoteRunner")
    @patch("agento.framework.consumer.get_connection")
    def test_run_job_no_resume_on_first_attempt(
        self, mock_conn, MockRunner, mock_get_ch, mock_get_wf,
        sample_config, sample_db_config, sample_consumer_config,
    ):
        """First attempt (attempt=1) never resumes, even if session_id is somehow set."""
        mock_conn.return_value = MagicMock()
        mock_result = _make_claude_result()
        mock_workflow = MagicMock()
        mock_workflow.execute_job.return_value = mock_result
        mock_get_wf.return_value.return_value = mock_workflow
        mock_get_ch.return_value = MagicMock(name="jira")

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(
            type=AgentType.CRON,
            reference_id="AI-1",
            attempt=1,
            session_id="sess-abc",
        )

        consumer._run_job(job)

        # No resume request: the workflow drives the run instead.
        mock_runner = MockRunner.return_value
        mock_runner.execute.assert_not_called()
        mock_workflow.execute_job.assert_called_once()


# ---- Section 7: Finalization ----


class TestFinalize:
    @patch("agento.framework.consumer.get_connection")
    def test_finalize_success(self, mock_get_conn, sample_config, sample_db_config, sample_consumer_config):
        mock_conn, mock_cursor = _mock_connection(row={"status": "RUNNING"})
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(attempt=1)
        job_result = _JobResult(
            summary="done",
            agent_type="claude",
            provider="anthropic",
            model="claude-sonnet-4",
            input_tokens=100,
            output_tokens=50,
            prompt="the prompt",
            output='{"result": "ok"}',
        )

        consumer._finalize_job(job, error=None, job_result=job_result, elapsed_ms=1000)

        sql_arg = _last_job_update(mock_cursor)[0]
        assert "SUCCESS" in sql_arg
        assert "agent_type" in sql_arg
        assert "provider" in sql_arg
        assert "model" in sql_arg
        assert "prompt" in sql_arg
        assert "output" in sql_arg
        params = _last_job_update(mock_cursor)[1]
        assert params[0] == "done"               # result_summary
        assert params[1] == "claude"             # agent_type (= harness id)
        assert params[2] == "anthropic"          # provider (model vendor)
        assert params[3] == "claude-sonnet-4"    # model
        assert params[4] == 100                  # input_tokens
        assert params[5] == 50                   # output_tokens
        assert params[6] == "the prompt"         # prompt
        assert params[7] == '{"result": "ok"}'   # output
        mock_conn.commit.assert_called_once()

    @patch("agento.framework.consumer.get_connection")
    def test_finalize_success_with_none_result(self, mock_get_conn, sample_config, sample_db_config, sample_consumer_config):
        mock_conn, mock_cursor = _mock_connection(row={"status": "RUNNING"})
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(attempt=1)

        consumer._finalize_job(job, error=None, job_result=None, elapsed_ms=1000)

        params = _last_job_update(mock_cursor)[1]
        assert params[0] is None  # result_summary
        assert params[1] is None  # agent_type
        assert params[2] is None  # model

    @patch("agento.framework.consumer.time.sleep")
    @patch("agento.framework.consumer.get_connection")
    def test_finalize_retries_a_failed_checkout(self, mock_get_conn, _sleep, sample_db_config, sample_consumer_config):
        """A connection that cannot be opened is one failed DB attempt, not the end of finalize."""
        mock_conn, _cursor = _mock_connection(row={"status": "RUNNING"})
        mock_get_conn.side_effect = [RuntimeError("db down"), mock_conn]

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        consumer._finalize_job(_make_job(attempt=1), error=None, job_result=None, elapsed_ms=1)

        mock_conn.commit.assert_called_once()

    @patch("agento.framework.consumer.evaluate_retry")
    @patch("agento.framework.consumer.get_connection")
    def test_finalize_retryable_failure(self, mock_get_conn, mock_eval, sample_config, sample_db_config, sample_consumer_config):
        from agento.framework.retry_policy import RetryDecision

        mock_eval.return_value = RetryDecision(should_retry=True, delay_seconds=60, reason="retry")

        mock_conn, mock_cursor = _mock_connection(row={"status": "RUNNING"})
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(attempt=1)

        consumer._finalize_job(
            job, error=RuntimeError("timeout"), job_result=None, elapsed_ms=5000
        )

        sql_arg = _last_job_update(mock_cursor)[0]
        assert "TODO" in sql_arg
        assert "scheduled_after" in sql_arg
        assert "session_id" in sql_arg  # session_id COALESCE in retry SQL
        mock_conn.commit.assert_called_once()

    @patch("agento.framework.consumer.evaluate_retry")
    @patch("agento.framework.consumer.get_connection")
    def test_finalize_retry_extracts_session_id_from_error(self, mock_get_conn, mock_eval, sample_config, sample_db_config, sample_consumer_config):
        from agento.framework.retry_policy import RetryDecision

        mock_eval.return_value = RetryDecision(should_retry=True, delay_seconds=60, reason="retry")

        mock_conn, mock_cursor = _mock_connection(row={"status": "RUNNING"})
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(attempt=1)

        error = RuntimeError("timeout")
        error.session_id = "sess-from-error"  # type: ignore[attr-defined]
        consumer._finalize_job(job, error=error, job_result=None, elapsed_ms=5000)

        params = _last_job_update(mock_cursor)[1]
        # session_id should be extracted from error
        assert "sess-from-error" in params

    @patch("agento.framework.consumer.evaluate_retry")
    @patch("agento.framework.consumer.get_connection")
    def test_finalize_usage_limit_pool_exhausted_reschedules_not_dead(
        self, mock_get_conn, mock_eval, sample_config, sample_db_config, sample_consumer_config
    ):
        """AG-46: a usage limit with the whole pool throttled reschedules the job to
        TODO (waiting for quota) instead of dead-lettering it, and refunds the attempt."""
        from datetime import UTC, datetime, timedelta

        from agento.framework.agent_manager.errors import UsageLimitError
        from agento.framework.retry_policy import RetryDecision

        # No failover left, so the retry policy says don't retry — the pool-wait branch
        # must take over from the DEAD branch.
        mock_eval.return_value = RetryDecision(
            should_retry=False, delay_seconds=0, reason="Non-retryable error: UsageLimitError"
        )
        mock_conn, mock_cursor = _mock_connection(row={"status": "RUNNING"})
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(attempt=3, max_attempts=3)

        reset = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
        error = UsageLimitError("usage limit reached")
        error.pool_retry_at = reset  # set by _handle_usage_limit when the pool is dry

        consumer._finalize_job(job, error=error, job_result=None, elapsed_ms=100)

        sql_arg = _statements(mock_cursor)[-1][0][0]
        assert "DEAD" not in sql_arg
        assert "TODO" in sql_arg
        assert "scheduled_after" in sql_arg
        # Attempt is refunded so quota waits never march the job toward max_attempts.
        assert "attempt = GREATEST(attempt - 1, 0)" in sql_arg
        # scheduled_after lands after the reset (reset + jitter of 60-300s).
        params = _statements(mock_cursor)[-1][0][1]
        scheduled_after = params[4]
        assert scheduled_after > reset
        assert scheduled_after <= reset + timedelta(seconds=300)
        mock_conn.commit.assert_called_once()

    @patch("agento.framework.consumer.evaluate_retry")
    @patch("agento.framework.consumer.get_connection")
    def test_finalize_pool_retry_at_wins_over_backoff_retry(
        self, mock_get_conn, mock_eval, sample_config, sample_db_config, sample_consumer_config
    ):
        """AG-46 (busy pool): a CredentialsBusyError is a retryable RuntimeError, so the
        retry policy says retry — but a known pool-recovery time must win over blind
        backoff. The job waits for the lease to free (reschedule + refund), not a fixed
        delay, and never dead-letters."""
        from datetime import UTC, datetime, timedelta

        from agento.framework.agent_manager.errors import CredentialsBusyError
        from agento.framework.retry_policy import RetryDecision

        # Retryable per policy (RuntimeError subclass) — the pool-wait branch must still
        # take priority because we know exactly when the pool recovers.
        mock_eval.return_value = RetryDecision(
            should_retry=True, delay_seconds=60, reason="Retry 2/3 after 60s"
        )
        mock_conn, mock_cursor = _mock_connection(row={"status": "RUNNING"})
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(attempt=1)

        lease_expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
        error = CredentialsBusyError("all healthy tokens leased", pool_retry_at=lease_expiry)

        consumer._finalize_job(job, error=error, job_result=None, elapsed_ms=2000)

        sql_arg = _statements(mock_cursor)[-1][0][0]
        assert "DEAD" not in sql_arg
        assert "TODO" in sql_arg
        # Refunded: waiting for a leased token is not a real failure.
        assert "attempt = GREATEST(attempt - 1, 0)" in sql_arg
        params = _statements(mock_cursor)[-1][0][1]
        scheduled_after = params[4]
        # Rescheduled for after the lease frees (lease_expiry + 60-300s jitter), NOT the
        # 60s backoff the retry policy proposed.
        assert scheduled_after > lease_expiry
        assert scheduled_after <= lease_expiry + timedelta(seconds=300)
        mock_conn.commit.assert_called_once()

    @patch("agento.framework.consumer.evaluate_retry")
    @patch("agento.framework.consumer.get_connection")
    def test_finalize_busy_pool_without_recovery_time_uses_backoff(
        self, mock_get_conn, mock_eval, sample_config, sample_db_config, sample_consumer_config
    ):
        """Pure row-lock contention leaves ``pool_retry_at`` None: the job falls back to
        the ordinary backoff retry (no attempt refund), preserving prior behaviour rather
        than dead-lettering."""
        from datetime import UTC, datetime, timedelta

        from agento.framework.agent_manager.errors import CredentialsBusyError
        from agento.framework.retry_policy import RetryDecision

        mock_eval.return_value = RetryDecision(
            should_retry=True, delay_seconds=60, reason="Retry 2/3 after 60s"
        )
        mock_conn, mock_cursor = _mock_connection(row={"status": "RUNNING"})
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(attempt=1)

        error = CredentialsBusyError("all healthy tokens locked", pool_retry_at=None)
        consumer._finalize_job(job, error=error, job_result=None, elapsed_ms=2000)

        # The ordinary retry also revokes the job's capabilities, so the job UPDATE is not
        # the last statement.
        update = next(
            c for c in reversed(_statements(mock_cursor)) if "UPDATE job" in c[0][0]
        )
        sql_arg = update[0][0]
        assert "TODO" in sql_arg
        assert "DEAD" not in sql_arg
        # Ordinary backoff retry, so no attempt refund.
        assert "GREATEST(attempt - 1, 0)" not in sql_arg
        params = update[0][1]
        scheduled_after = params[4]
        # ~60s backoff from now, not a lease-derived wait.
        assert scheduled_after <= datetime.now(UTC) + timedelta(seconds=120)
        mock_conn.commit.assert_called_once()

    @patch("agento.framework.consumer.evaluate_retry")
    @patch("agento.framework.consumer.get_connection")
    def test_finalize_error_message_truncated(self, mock_get_conn, mock_eval, sample_config, sample_db_config, sample_consumer_config):
        from agento.framework.retry_policy import RetryDecision

        mock_eval.return_value = RetryDecision(
            should_retry=False, delay_seconds=0, reason="dead"
        )

        mock_conn, mock_cursor = _mock_connection(row={"status": "RUNNING"})
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(attempt=1)

        long_error = RuntimeError("x" * 3000)
        consumer._finalize_job(job, error=long_error, job_result=None, elapsed_ms=100)

        params = _last_job_update(mock_cursor)[1]
        error_msg = params[0]
        assert len(error_msg) <= 2000

    @patch("agento.framework.consumer.get_connection")
    def test_finalize_db_error_does_not_crash(self, mock_get_conn, sample_config, sample_db_config, sample_consumer_config):
        mock_conn, mock_cursor = _mock_connection()
        mock_cursor.execute.side_effect = _fails_except_pool(RuntimeError("DB down"))
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(attempt=1)
        job_result = _JobResult(summary="ok")

        # Should not raise (retries 3 times then gives up)
        consumer._finalize_job(job, error=None, job_result=job_result, elapsed_ms=100)

        assert len(_statements(mock_cursor)) == 3  # 3 retry attempts

    @patch("agento.framework.consumer.time.sleep")
    @patch("agento.framework.consumer.get_connection")
    def test_finalize_retries_on_db_error_then_succeeds(self, mock_get_conn, mock_sleep, sample_config, sample_db_config, sample_consumer_config):
        """DB fails on first attempt, succeeds on second."""
        conn, cursor = _mock_connection(row={"status": "RUNNING"})
        cursor.execute.side_effect = [RuntimeError("DB down")] + [None] * 20
        mock_get_conn.return_value = conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(attempt=1)
        job_result = _JobResult(summary="ok")

        consumer._finalize_job(job, error=None, job_result=job_result, elapsed_ms=100)

        conn.commit.assert_called_once()
        mock_sleep.assert_called_once_with(1)

    @patch("agento.framework.consumer.get_connection")
    def test_finalize_skips_when_job_no_longer_running(
        self, mock_get_conn, sample_config, sample_db_config, sample_consumer_config,
    ):
        """If the job was paused during execution, finalize must NOT overwrite PAUSED status."""
        mock_conn, mock_cursor = _mock_connection(row={"status": "PAUSED"})
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(attempt=1)
        job_result = _JobResult(summary="ok")

        consumer._finalize_job(job, error=None, job_result=job_result, elapsed_ms=100)

        # No UPDATE to SUCCESS/DEAD/TODO — the job row is not ours to write any more.
        assert not [c for c in _statements(mock_cursor) if "UPDATE job" in c[0][0]]
        assert "SELECT status" in _statements(mock_cursor)[0][0][0]
        # It DOES commit: this attempt's execution is abandoned, because the process it
        # described has exited (§5.3). With no finalizer registered that commit is empty.
        mock_conn.commit.assert_called_once()


# ---- Section 8: Lifecycle ----


class TestLifecycle:
    def test_shutdown_on_sigterm(self, sample_config, sample_db_config, sample_consumer_config):
        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        assert not consumer._shutdown.is_set()

        consumer._handle_signal(signal.SIGTERM, None)

        assert consumer._shutdown.is_set()

    @patch("agento.framework.consumer.get_connection")
    def test_poll_loop_exits_on_shutdown(self, mock_get_conn, sample_config, sample_db_config, sample_consumer_config):
        mock_conn, _mock_cursor = _mock_connection(row=None)
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        consumer._shutdown.set()  # Pre-set shutdown

        # run() should exit quickly without blocking
        consumer.run()


# ---- Section 9: PID & Session helpers ----


class TestSavePid:
    @patch("agento.framework.consumer.get_connection")
    def test_save_pid_updates_db(self, mock_get_conn, sample_db_config, sample_consumer_config):
        mock_conn, mock_cursor = _mock_connection()
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        consumer._save_pid(42, 12345, "runner-2.sock:b1")

        assert len(_statements(mock_cursor)) == 1
        sql = _statements(mock_cursor)[0][0][0]
        assert "pid" in sql and "runner_ref" in sql
        params = _statements(mock_cursor)[0][0][1]
        assert params == (12345, "runner-2.sock:b1", 42)
        mock_conn.commit.assert_called_once()

    @patch("agento.framework.consumer.get_connection")
    def test_save_pid_db_error_does_not_crash(self, mock_get_conn, sample_db_config, sample_consumer_config):
        mock_get_conn.side_effect = RuntimeError("DB down")

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        # Should not raise
        consumer._save_pid(42, 12345)


class TestSaveSessionId:
    @patch("agento.framework.consumer.get_connection")
    def test_save_session_id_updates_db(self, mock_get_conn, sample_db_config, sample_consumer_config):
        mock_conn, mock_cursor = _mock_connection()
        mock_get_conn.return_value = mock_conn

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        consumer._save_session_id(42, "sess-abc")

        assert len(_statements(mock_cursor)) == 1
        sql = _statements(mock_cursor)[0][0][0]
        assert "session_id" in sql
        params = _statements(mock_cursor)[0][0][1]
        assert params == ("sess-abc", 42)
        mock_conn.commit.assert_called_once()

    @patch("agento.framework.consumer.get_connection")
    def test_save_session_id_db_error_does_not_crash(self, mock_get_conn, sample_db_config, sample_consumer_config):
        mock_get_conn.side_effect = RuntimeError("DB down")

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        # Should not raise
        consumer._save_session_id(42, "sess-abc")


class TestJobResultSessionId:
    def test_from_run_result_captures_session_id(self):
        result = RunResult(raw_output="ok", session_id="sess-xyz")
        jr = _JobResult.from_run_result(result, "summary")
        assert jr.session_id == "sess-xyz"

    def test_from_run_result_no_session_id(self):
        result = RunResult(raw_output="ok")
        jr = _JobResult.from_run_result(result, "summary")
        assert jr.session_id is None
