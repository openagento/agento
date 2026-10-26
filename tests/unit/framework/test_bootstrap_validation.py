"""Tests for bootstrap protocol validation — bad classes get skipped."""
from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest

from agento.framework.bootstrap import bootstrap as _bootstrap
from agento.framework.channels import registry as channel_registry
from agento.framework.commands import clear as clear_commands
from agento.framework.commands import get_commands
from agento.framework.event_manager import clear as clear_event_manager
from agento.framework.event_manager import get_event_manager
from agento.framework.router_registry import clear as clear_routers
from agento.framework.router_registry import get_routers
from agento.framework.workflows import _WORKFLOW_MAP
from agento.framework.workflows import clear as clear_workflows

# The default user_dir is the real /app/code; on a deployed host it holds user modules.
bootstrap = partial(_bootstrap, user_dir="/nonexistent")


@pytest.fixture(autouse=True)
def _clean_registries():
    channel_registry.clear()
    clear_workflows()
    clear_commands()
    clear_event_manager()
    clear_routers()
    yield
    channel_registry.clear()
    clear_workflows()
    clear_commands()
    clear_event_manager()
    clear_routers()


def _write_module(modules_dir: Path, name: str, manifest: dict) -> Path:
    mod_dir = modules_dir / name
    mod_dir.mkdir(parents=True)
    (mod_dir / "module.json").write_text(json.dumps(manifest))
    return mod_dir


class TestBootstrapValidation:
    def test_bad_command_missing_shortcut_skipped(self, tmp_path: Path):
        """Command class missing shortcut property is skipped."""
        mod_dir = _write_module(tmp_path, "bad-cmd", {
            "name": "bad-cmd",
            "provides": {
                "commands": [{"name": "bad", "class": "src.cmd.BadCommand"}],
            },
        })
        src = mod_dir / "src"
        src.mkdir()
        (src / "cmd.py").write_text(
            "class BadCommand:\n"
            "    @property\n"
            "    def name(self): return 'bad'\n"
            "    @property\n"
            "    def help(self): return 'bad'\n"
            "    def configure(self, parser): pass\n"
            "    def execute(self, args): pass\n"
            "    # Missing shortcut property\n"
        )

        bootstrap(str(tmp_path))
        assert "bad" not in get_commands()

    def test_valid_command_with_shortcut_registered(self, tmp_path: Path):
        """Command with all required properties is registered."""
        mod_dir = _write_module(tmp_path, "good-cmd", {
            "name": "good-cmd",
            "provides": {
                "commands": [{"name": "good", "class": "src.cmd.GoodCommand"}],
            },
        })
        src = mod_dir / "src"
        src.mkdir()
        (src / "cmd.py").write_text(
            "class GoodCommand:\n"
            "    @property\n"
            "    def name(self): return 'good'\n"
            "    @property\n"
            "    def shortcut(self): return 'go'\n"
            "    @property\n"
            "    def help(self): return 'good cmd'\n"
            "    def configure(self, parser): pass\n"
            "    def execute(self, args): pass\n"
        )

        bootstrap(str(tmp_path))
        assert "good" in get_commands()

    def test_bad_channel_missing_method_skipped(self, tmp_path: Path):
        """Channel class missing required method is skipped."""
        mod_dir = _write_module(tmp_path, "bad-ch", {
            "name": "bad-ch",
            "provides": {
                "channels": [{"name": "bad", "class": "src.ch.BadChannel"}],
            },
        })
        src = mod_dir / "src"
        src.mkdir()
        (src / "ch.py").write_text(
            "class BadChannel:\n"
            "    @property\n"
            "    def name(self): return 'bad'\n"
            "    # Missing get_prompt_fragments and get_followup_fragments\n"
        )

        bootstrap(str(tmp_path))
        with pytest.raises(ValueError, match="Unknown channel"):
            channel_registry.get_channel("bad")

    def test_bad_observer_missing_execute_skipped(self, tmp_path: Path):
        """Observer class missing execute method is skipped."""
        mod_dir = _write_module(tmp_path, "bad-obs", {"name": "bad-obs"})
        src = mod_dir / "src"
        src.mkdir()
        (src / "obs.py").write_text(
            "class BadObserver:\n"
            "    pass  # Missing execute method\n"
        )
        (mod_dir / "events.json").write_text(json.dumps({
            "job_fail_after": [{"name": "bad", "class": "src.obs.BadObserver"}],
        }))

        bootstrap(str(tmp_path))
        assert get_event_manager().observer_count("job_fail_after") == 0

    def test_bad_workflow_not_subclass_skipped(self, tmp_path: Path):
        """Workflow that does not extend Workflow base class is skipped."""
        mod_dir = _write_module(tmp_path, "bad-wf", {
            "name": "bad-wf",
            "provides": {
                "workflows": [{"type": "cron", "class": "src.wf.NotAWorkflow"}],
            },
        })
        src = mod_dir / "src"
        src.mkdir()
        (src / "wf.py").write_text(
            "class NotAWorkflow:\n"
            "    def build_prompt(self, channel, ref, **kw): return 'test'\n"
        )

        bootstrap(str(tmp_path))
        # BlankWorkflow is always registered, but CRON should not be
        from agento.framework.job_models import AgentType
        assert AgentType.CRON not in _WORKFLOW_MAP

    def test_bad_router_missing_resolve_skipped(self, tmp_path: Path):
        """Router class missing resolve method is skipped."""
        mod_dir = _write_module(tmp_path, "bad-rt", {
            "name": "bad-rt",
            "provides": {
                "routers": [{"name": "bad", "class": "src.rt.BadRouter"}],
            },
        })
        src = mod_dir / "src"
        src.mkdir()
        (src / "rt.py").write_text(
            "class BadRouter:\n"
            "    @property\n"
            "    def name(self): return 'bad'\n"
            "    # Missing resolve method\n"
        )

        bootstrap(str(tmp_path))
        assert len(get_routers()) == 0

    def test_valid_channel_still_registers(self, tmp_path: Path):
        """Valid channel is not broken by validation."""
        mod_dir = _write_module(tmp_path, "valid-ch", {
            "name": "valid-ch",
            "provides": {
                "channels": [{"name": "valid", "class": "src.ch.ValidChannel"}],
            },
        })
        src = mod_dir / "src"
        src.mkdir()
        (src / "ch.py").write_text(
            "class ValidChannel:\n"
            "    @property\n"
            "    def name(self): return 'valid'\n"
            "    def get_prompt_fragments(self, ref): pass\n"
            "    def get_followup_fragments(self, ref, instr): pass\n"
        )

        bootstrap(str(tmp_path))
        assert channel_registry.get_channel("valid").name == "valid"
