"""The consumer resolves the sessions a run may resume BEFORE it builds the run dir, so
copy-in gives the run HOME only those (`copy_in_persistent_paths`)."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.consumer import Consumer
from tests.unit.framework.test_consumer_agent_view import (
    _make_claude_result,
    _make_job,
    _make_runtime_with_agent_view,
)

pytestmark = pytest.mark.usefixtures("builtin_harnesses")


@pytest.fixture(autouse=True)
def _stubs():
    with patch.object(Consumer, "_issue_run_capabilities", return_value=("cap-mcp", None)), \
         patch("agento.framework.consumer.CredentialResolver"), \
         patch("agento.framework.consumer.get_connection", return_value=MagicMock()), \
         patch("agento.framework.consumer.resolve_agent_view_runtime",
               return_value=_make_runtime_with_agent_view()), \
         patch("agento.framework.consumer.get_channel"), \
         patch("agento.framework.consumer.RemoteRunner"):
        yield


@pytest.fixture
def run(tmp_path, sample_db_config, sample_consumer_config):
    """Run `job` with `_resume_session` answering `line_sid`; returns the mocks to inspect."""
    def _run(job, line_sid=None):
        workflow = MagicMock()
        workflow.execute_job.return_value = _make_claude_result()
        with patch.object(Consumer, "_resume_session", return_value=line_sid), \
             patch("agento.framework.consumer.materialize_run_workspace",
                   return_value=(tmp_path, tmp_path)) as materialize, \
             patch("agento.framework.consumer.save_persistent_state") as save, \
             patch("agento.framework.consumer.get_workflow_class") as get_wf:
            get_wf.return_value.return_value = workflow
            Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))._run_job(job)
        return materialize, save, workflow
    return _run


def _session(home, sid):
    path = home / ".claude" / "projects" / "-workspace-x" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")


def test_run_job_passes_line_resume_session_to_materialize(run, tmp_path):
    _session(tmp_path, "s-line")
    materialize, _, workflow = run(_make_job(agent_view_id=2), line_sid="s-line")
    assert materialize.call_args.kwargs["resume_session_ids"] == ("s-line",)
    assert workflow.execute_job.call_args.args[2].resume_session_id == "s-line"


def test_run_job_passes_retry_session_to_materialize(run):
    materialize, _, _ = run(_make_job(agent_view_id=2, attempt=2, session_id="s9"))
    assert materialize.call_args.kwargs["resume_session_ids"] == ("s9",)


def test_run_job_session_missing_in_state_starts_fresh(run, caplog):
    """Copy-in found no file in state/, so the run HOME has none: a fresh session."""
    with caplog.at_level(logging.INFO):
        _, _, workflow = run(_make_job(agent_view_id=2), line_sid="s-gone")
    assert workflow.execute_job.call_args.args[2].resume_session_id is None
    assert "Job 42: session s-gone has no file, starting a fresh session" in caplog.text


def test_run_job_saves_with_session_ids(run, tmp_path):
    _session(tmp_path, "s-line")
    _, save, _ = run(_make_job(agent_view_id=2, attempt=2, session_id="s9"), line_sid="s-line")
    assert save.call_args.kwargs["session_ids"] == ("s9", "s-line", "s9")
