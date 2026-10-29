from __future__ import annotations

import signal
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.job_models import JobStatus
from agento.framework.job_store import fetch_job, pause_job, resume_job, set_job_runtime


def _make_row(**overrides) -> dict:
    row = {
        "id": 42,
        "schedule_id": None,
        "type": "cron",
        "source": "jira",
        "agent_view_id": 1,
        "priority": 50,
        "reference_id": "AI-42",
        "agent_type": None,
        "model": None,
        "input_tokens": None,
        "output_tokens": None,
        "prompt": None,
        "output": None,
        "context": None,
        "idempotency_key": "jira:cron:AI-42:20260220_0800",
        "status": "RUNNING",
        "attempt": 1,
        "max_attempts": 3,
        "scheduled_after": datetime(2026, 2, 20, 8, 0),
        "started_at": datetime(2026, 2, 20, 8, 0, 5),
        "finished_at": None,
        "result_summary": None,
        "error_message": None,
        "error_class": None,
        "pid": 12345,
        "session_id": "sess-abc-123",
        "created_at": datetime(2026, 2, 20, 7, 59),
        "updated_at": datetime(2026, 2, 20, 8, 0, 5),
    }
    row.update(overrides)
    return row


def _mock_conn(row=None, rowcount=1):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    cursor.rowcount = rowcount
    conn.cursor.return_value.__enter__ = lambda s: cursor
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


class TestFetchJob:
    def test_returns_job_when_found(self):
        conn, _cursor = _mock_conn(_make_row())
        job = fetch_job(conn, 42)
        assert job is not None
        assert job.id == 42
        assert job.status == JobStatus.RUNNING

    def test_returns_none_when_not_found(self):
        conn, _cursor = _mock_conn(None)
        assert fetch_job(conn, 999) is None


class TestPauseJob:
    def test_pause_running_job_with_live_pid(self):
        """The SIGTERM goes through the run's owner, for this job's tag only (WS5)."""
        conn, _cursor = _mock_conn(_make_row(pid=12345, runner_ref="runner-1.sock:b1"))

        with patch("agento.framework.job_store.runner_client.signal", return_value=True) as sig, \
                patch("agento.framework.job_store.runner_client.alive", side_effect=["alive", "dead"]), \
                patch("agento.framework.job_store.time.sleep"):
            job = pause_job(conn, 42)

        assert job.status == JobStatus.PAUSED
        sig.assert_called_once_with("runner-1.sock:b1", "job:42", signal.SIGTERM)
        conn.commit.assert_called_once()

    def test_pause_running_job_with_dead_pid(self):
        conn, _cursor = _mock_conn(_make_row(pid=99999, runner_ref="runner-1.sock:b1"))

        with patch("agento.framework.job_store.runner_client.signal", return_value=False), \
                patch("agento.framework.job_store.runner_client.alive") as alive:
            job = pause_job(conn, 42)

        assert job.status == JobStatus.PAUSED
        alive.assert_not_called()
        conn.commit.assert_called_once()

    def test_pause_running_job_without_pid(self):
        conn, _cursor = _mock_conn(_make_row(pid=None))
        job = pause_job(conn, 42)
        assert job.status == JobStatus.PAUSED

    def test_pause_not_found_raises(self):
        conn, _cursor = _mock_conn(None)
        with pytest.raises(ValueError, match="Job not found"):
            pause_job(conn, 999)

    def test_pause_wrong_status_raises(self):
        conn, _cursor = _mock_conn(_make_row(status="SUCCESS"))
        with pytest.raises(ValueError, match="Cannot pause job in status SUCCESS"):
            pause_job(conn, 42)

    def test_pause_paused_job_raises(self):
        conn, _cursor = _mock_conn(_make_row(status="PAUSED"))
        with pytest.raises(ValueError, match="Cannot pause job in status PAUSED"):
            pause_job(conn, 42)

    def test_pause_race_status_changed_raises(self):
        """If the job transitions RUNNING → SUCCESS between fetch and UPDATE,
        rowcount is 0 and pause_job must raise instead of lying."""
        # First fetch: status=RUNNING. UPDATE affects 0 rows (raced).
        # Second fetch (post-UPDATE re-read): status=SUCCESS.
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            _make_row(status="RUNNING"),
            _make_row(status="SUCCESS"),
        ]
        cursor.rowcount = 0
        conn.cursor.return_value.__enter__ = lambda s: cursor
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(ValueError, match="status changed to SUCCESS"):
            pause_job(conn, 42)


class TestResumeJob:
    def test_resume_paused_job(self):
        conn, _cursor = _mock_conn(_make_row(status="PAUSED", pid=12345, session_id="sess-abc"))
        job = resume_job(conn, 42)
        assert job.status == JobStatus.TODO
        assert job.pid is None
        conn.commit.assert_called_once()

    def test_resume_not_found_raises(self):
        conn, _cursor = _mock_conn(None)
        with pytest.raises(ValueError, match="Job not found"):
            resume_job(conn, 999)

    def test_resume_wrong_status_raises(self):
        conn, _cursor = _mock_conn(_make_row(status="RUNNING"))
        with pytest.raises(ValueError, match="Cannot resume job in status RUNNING"):
            resume_job(conn, 42)

    @pytest.mark.parametrize("state", ["alive", "unknown"])
    def test_resume_refused_until_owner_answers_dead(self, state):
        """A paused run whose child ignored SIGTERM, or whose runner does not answer,
        keeps its owner: resume must not clear it and start the same session twice."""
        conn, _cursor = _mock_conn(_make_row(status="PAUSED", runner_ref="runner-1.sock:b1"))
        with patch("agento.framework.job_store.runner_client.alive", return_value=state), \
                pytest.raises(ValueError, match=state):
            resume_job(conn, 42)
        conn.commit.assert_not_called()

    def test_resume_no_session_id_raises(self):
        conn, _cursor = _mock_conn(_make_row(status="PAUSED", session_id=None))
        with pytest.raises(ValueError, match="no session_id"):
            resume_job(conn, 42)


class TestSetJobRuntime:
    """`job:set-runtime` — pin/clear a queued job's harness and model."""

    def _todo(self, **overrides):
        return _make_row(status="TODO", **overrides)

    def test_pins_harness_and_model_and_clears_provider(self):
        conn, cursor = _mock_conn(self._todo(agent_type=None, model=None))
        job = set_job_runtime(conn, 42, harness="codex", model="gpt-x")
        sql, params = cursor.execute.call_args_list[-1][0]
        assert "provider = NULL" in sql
        assert params[:2] == ("codex", "gpt-x")
        assert (job.agent_type, job.model, job.provider) == ("codex", "gpt-x", None)

    def test_clear_nulls_both(self):
        conn, cursor = _mock_conn(self._todo(agent_type="codex", model="gpt-x"))
        job = set_job_runtime(conn, 42, clear=True)
        params = cursor.execute.call_args_list[-1][0][1]
        assert params[:2] == (None, None)
        assert (job.agent_type, job.model) == (None, None)

    def test_repeated_identical_update_succeeds_even_though_nothing_changes(self):
        # PyMySQL reports CHANGED rows, so an identical edit affects 0 rows. The status
        # check is the error source, not rowcount.
        conn, _cursor = _mock_conn(self._todo(agent_type="codex", model="gpt-x"), rowcount=0)
        job = set_job_runtime(conn, 42, harness="codex", model="gpt-x")
        assert job.agent_type == "codex"

    def test_repeated_clear_succeeds(self):
        conn, _cursor = _mock_conn(self._todo(agent_type=None, model=None), rowcount=0)
        assert set_job_runtime(conn, 42, clear=True).agent_type is None

    def test_rejects_a_job_that_is_not_todo(self):
        conn, _cursor = _mock_conn(_make_row(status="RUNNING"))
        with pytest.raises(ValueError, match="not TODO"):
            set_job_runtime(conn, 42, harness="codex")

    def test_rejects_a_missing_job(self):
        conn, _cursor = _mock_conn(None)
        with pytest.raises(ValueError, match="not found"):
            set_job_runtime(conn, 42, harness="codex")

    @pytest.mark.parametrize(("stored", "dry_run", "written"), [
        (0, True, 1), (1, False, 0), (1, None, 1), (0, None, 0),
    ])
    def test_dry_run_is_set_or_kept(self, stored, dry_run, written):
        conn, cursor = _mock_conn(self._todo(dry_run=stored))
        job = set_job_runtime(conn, 42, dry_run=dry_run)
        sql, params = cursor.execute.call_args_list[-1][0]
        assert "dry_run = %s" in sql
        assert params[3] == written
        assert job.dry_run is bool(written)

    def test_changing_the_harness_drops_the_session(self):
        conn, cursor = _mock_conn(self._todo(agent_type="claude", session_id="sess-1"))
        job = set_job_runtime(conn, 42, harness="codex")
        assert cursor.execute.call_args_list[-1][0][1][2] is True
        assert job.session_id is None

    def test_clear_drops_the_session(self):
        conn, cursor = _mock_conn(self._todo(agent_type="claude", session_id="sess-1"))
        job = set_job_runtime(conn, 42, clear=True)
        assert cursor.execute.call_args_list[-1][0][1][2] is True
        assert job.session_id is None

    def test_an_identical_harness_keeps_the_session(self):
        conn, cursor = _mock_conn(self._todo(agent_type="claude", session_id="sess-1"))
        job = set_job_runtime(conn, 42, harness="claude", model="m")
        assert cursor.execute.call_args_list[-1][0][1][2] is False
        assert job.session_id == "sess-1"

    def test_a_model_only_edit_keeps_the_session(self):
        conn, cursor = _mock_conn(self._todo(agent_type="claude", session_id="sess-1"))
        job = set_job_runtime(conn, 42, model="m")
        assert cursor.execute.call_args_list[-1][0][1][2] is False
        assert job.session_id == "sess-1"
