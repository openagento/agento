from __future__ import annotations

import argparse
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.job_models import AgentType, Job, JobStatus


def _make_job(**overrides) -> Job:
    defaults = dict(
        id=42,
        schedule_id=None,
        type=AgentType.CRON,
        source="jira",
        agent_view_id=1,
        priority=50,
        reference_id="AI-42",
        agent_type=None,
        provider=None,
        model=None,
        input_tokens=None,
        output_tokens=None,
        prompt=None,
        output=None,
        context=None,
        idempotency_key="jira:cron:AI-42:20260220_0800",
        status=JobStatus.RUNNING,
        attempt=1,
        max_attempts=3,
        scheduled_after=datetime(2026, 2, 20, 8, 0),
        started_at=datetime(2026, 2, 20, 8, 0, 5),
        finished_at=None,
        result_summary=None,
        error_message=None,
        error_class=None,
        pid=12345,
        session_id="sess-abc-123",
        created_at=datetime(2026, 2, 20, 7, 59),
        updated_at=datetime(2026, 2, 20, 8, 0, 5),
    )
    defaults.update(overrides)
    return Job(**defaults)


class TestPauseCommand:
    @patch("agento.framework.event_manager.get_event_manager")
    @patch("agento.framework.job_store.pause_job")
    @patch("agento.framework.cli.runtime.get_connection_or_exit")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_pause_success(self, mock_config, mock_conn, mock_pause, mock_em, capsys):
        from agento.framework.cli.runtime import PauseCommand

        mock_config.return_value = (MagicMock(), MagicMock(), MagicMock())
        conn = MagicMock()
        mock_conn.return_value = conn

        job = _make_job(status=JobStatus.PAUSED)
        mock_pause.return_value = job

        cmd = PauseCommand()
        args = argparse.Namespace(job_id=42)
        cmd.execute(args)

        mock_pause.assert_called_once_with(conn, 42)
        mock_em.return_value.dispatch.assert_called_once()
        event_name = mock_em.return_value.dispatch.call_args[0][0]
        assert event_name == "job_pause_after"

        captured = capsys.readouterr()
        assert "Job #42 paused" in captured.out
        assert "sess-abc-123" in captured.out

    @patch("agento.framework.job_store.pause_job")
    @patch("agento.framework.cli.runtime.get_connection_or_exit")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_pause_wrong_status_exits(self, mock_config, mock_conn, mock_pause):
        from agento.framework.cli.runtime import PauseCommand

        mock_config.return_value = (MagicMock(), MagicMock(), MagicMock())
        mock_conn.return_value = MagicMock()
        mock_pause.side_effect = ValueError("Cannot pause job in status SUCCESS")

        cmd = PauseCommand()
        args = argparse.Namespace(job_id=42)
        with pytest.raises(SystemExit) as exc_info:
            cmd.execute(args)
        assert exc_info.value.code == 1

    @patch("agento.framework.job_store.pause_job")
    @patch("agento.framework.cli.runtime.get_connection_or_exit")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_pause_not_found_exits(self, mock_config, mock_conn, mock_pause):
        from agento.framework.cli.runtime import PauseCommand

        mock_config.return_value = (MagicMock(), MagicMock(), MagicMock())
        mock_conn.return_value = MagicMock()
        mock_pause.side_effect = ValueError("Job not found: id=999")

        cmd = PauseCommand()
        args = argparse.Namespace(job_id=999)
        with pytest.raises(SystemExit) as exc_info:
            cmd.execute(args)
        assert exc_info.value.code == 1


class TestResumeCommand:
    @patch("agento.framework.event_manager.get_event_manager")
    @patch("agento.framework.job_store.resume_job")
    @patch("agento.framework.cli.runtime.get_connection_or_exit")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_resume_success(self, mock_config, mock_conn, mock_resume, mock_em, capsys):
        from agento.framework.cli.runtime import ResumeCommand

        mock_config.return_value = (MagicMock(), MagicMock(), MagicMock())
        conn = MagicMock()
        mock_conn.return_value = conn

        job = _make_job(status=JobStatus.TODO, pid=None)
        mock_resume.return_value = job

        cmd = ResumeCommand()
        args = argparse.Namespace(job_id=42)
        cmd.execute(args)

        mock_resume.assert_called_once_with(conn, 42)
        mock_em.return_value.dispatch.assert_called_once()
        event_name = mock_em.return_value.dispatch.call_args[0][0]
        assert event_name == "job_resume_after"

        captured = capsys.readouterr()
        assert "Job #42 re-queued" in captured.out
        assert "sess-abc-123" in captured.out

    @patch("agento.framework.job_store.resume_job")
    @patch("agento.framework.cli.runtime.get_connection_or_exit")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_resume_wrong_status_exits(self, mock_config, mock_conn, mock_resume):
        from agento.framework.cli.runtime import ResumeCommand

        mock_config.return_value = (MagicMock(), MagicMock(), MagicMock())
        mock_conn.return_value = MagicMock()
        mock_resume.side_effect = ValueError("Cannot resume job in status RUNNING")

        cmd = ResumeCommand()
        args = argparse.Namespace(job_id=42)
        with pytest.raises(SystemExit) as exc_info:
            cmd.execute(args)
        assert exc_info.value.code == 1

    @patch("agento.framework.job_store.resume_job")
    @patch("agento.framework.cli.runtime.get_connection_or_exit")
    @patch("agento.framework.cli.runtime._load_framework_config")
    def test_resume_no_session_exits(self, mock_config, mock_conn, mock_resume):
        from agento.framework.cli.runtime import ResumeCommand

        mock_config.return_value = (MagicMock(), MagicMock(), MagicMock())
        mock_conn.return_value = MagicMock()
        mock_resume.side_effect = ValueError("Cannot resume job 42: no session_id")

        cmd = ResumeCommand()
        args = argparse.Namespace(job_id=42)
        with pytest.raises(SystemExit) as exc_info:
            cmd.execute(args)
        assert exc_info.value.code == 1
