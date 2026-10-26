from __future__ import annotations

import pytest

from agento.framework.bootstrap import bootstrap
from agento.framework.channels import registry as channel_registry
from agento.framework.event_manager import clear as clear_event_manager
from agento.framework.job_models import AgentType
from agento.framework.workflows import _WORKFLOW_MAP, get_workflow_class
from agento.framework.workflows import clear as clear_workflows


@pytest.fixture(autouse=True)
def _clean_registries():
    channel_registry.clear()
    clear_workflows()
    clear_event_manager()
    yield
    channel_registry.clear()
    clear_workflows()
    clear_event_manager()


def test_bootstrap_registers_generic_workflows_with_no_modules(tmp_path):
    bootstrap(core_dir=str(tmp_path), user_dir="/nonexistent", db_conn=None)
    assert AgentType.BLANK.value in _WORKFLOW_MAP  # keyed by the type id (PRD E3-E5 §4.2)
    assert get_workflow_class(AgentType.TODO).__name__ == "TodoWorkflow"
    assert get_workflow_class(AgentType.FOLLOWUP).__name__ == "FollowupWorkflow"
