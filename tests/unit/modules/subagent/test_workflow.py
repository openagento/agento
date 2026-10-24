from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import agento.modules.subagent.src.channel as channel_module
from agento.framework.bootstrap import bootstrap
from agento.framework.channels import registry as channel_registry
from agento.framework.channels.base import Channel
from agento.framework.event_manager import clear as clear_event_manager
from agento.framework.harness import RunResult
from agento.framework.job_types import clear_job_types, resolve_job_type
from agento.framework.workflows import clear as clear_workflows
from agento.framework.workflows import get_workflow_class
from agento.modules.subagent.src.channel import SubagentChannel
from agento.modules.subagent.src.workflow import SubagentWorkflow

PROMPT = "Policz pliki w repo.\n\nZwróć tylko liczbę."


def _job(context):
    job = MagicMock()
    job.id, job.reference_id, job.context = 901, "42", context
    return job


def test_prompt_is_header_plus_verbatim_prompt():
    prompt = SubagentWorkflow(MagicMock(), MagicMock()).build_prompt(SubagentChannel(), "42", prompt=PROMPT)
    assert prompt == (
        "Task delegated by Agento job #42. Your final answer is returned to that job as your result."
        f"\n\n{PROMPT}"
    )


def test_execute_job_runs_the_built_prompt():
    runner = MagicMock()
    runner.execute.return_value = RunResult(raw_output="ok")
    SubagentWorkflow(runner, MagicMock()).execute_job(SubagentChannel(), _job(PROMPT), MagicMock())
    assert runner.execute.call_args.args[0].prompt.endswith(PROMPT)


@pytest.mark.parametrize("context", [None, ""])
def test_missing_context_raises(context):
    with pytest.raises(ValueError, match="no context"):
        SubagentWorkflow(MagicMock(), MagicMock()).execute_job(SubagentChannel(), _job(context), MagicMock())


def test_channel_satisfies_protocol():
    assert isinstance(SubagentChannel(), Channel)
    assert SubagentChannel().name == "subagent"


@pytest.fixture
def _clean_registries():
    channel_registry.clear()
    clear_workflows()
    clear_event_manager()
    clear_job_types()
    yield
    channel_registry.clear()
    clear_workflows()
    clear_event_manager()
    clear_job_types()


def test_di_json_registers_type_channel_and_workflow(tmp_path, _clean_registries):
    shutil.copytree(Path(channel_module.__file__).parents[1], tmp_path / "subagent")
    bootstrap(core_dir=str(tmp_path), user_dir="/nonexistent", db_conn=None)
    job_type = resolve_job_type("subagent")
    assert job_type.value == "subagent"
    assert get_workflow_class(job_type).__name__ == "SubagentWorkflow"
    assert channel_registry.get_channel("subagent").name == "subagent"
