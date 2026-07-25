"""Tests for bootstrap — module loading into registries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.framework.bootstrap import bootstrap, get_manifests
from agento.framework.channels import registry as channel_registry
from agento.framework.event_manager import clear as clear_event_manager
from agento.framework.event_manager import get_event_manager
from agento.framework.ingress_identity import clear_regex_identity_types, is_regex_identity_type
from agento.framework.job_models import AgentType
from agento.framework.workflows import _WORKFLOW_MAP
from agento.framework.workflows import clear as clear_workflows


@pytest.fixture(autouse=True)
def _clean_registries():
    """Reset registries before/after each test."""
    channel_registry.clear()
    clear_workflows()
    clear_event_manager()
    clear_regex_identity_types()
    yield
    channel_registry.clear()
    clear_workflows()
    clear_event_manager()
    clear_regex_identity_types()


def _write_module(modules_dir: Path, name: str, manifest: dict) -> Path:
    mod_dir = modules_dir / name
    mod_dir.mkdir(parents=True)
    (mod_dir / "module.json").write_text(json.dumps(manifest))
    return mod_dir


class TestBootstrap:
    def test_empty_modules_dir(self, tmp_path: Path):
        result = bootstrap(str(tmp_path))
        assert result == []
        # BlankWorkflow should still be registered
        assert AgentType.BLANK in _WORKFLOW_MAP

    def test_loads_channel_from_module(self, tmp_path: Path):
        mod_dir = _write_module(tmp_path, "test-ch", {
            "name": "test-ch",
            "provides": {
                "channels": [{"name": "test", "class": "src.channel.TestChannel"}],
            },
        })
        src = mod_dir / "src"
        src.mkdir()
        (src / "channel.py").write_text(
            "class TestChannel:\n"
            "    @property\n"
            "    def name(self): return 'test'\n"
            "    def get_prompt_fragments(self, ref): pass\n"
            "    def get_followup_fragments(self, ref, instr): pass\n"
        )

        bootstrap(str(tmp_path))

        ch = channel_registry.get_channel("test")
        assert ch.name == "test"

    def test_loads_workflow_from_module(self, tmp_path: Path):
        mod_dir = _write_module(tmp_path, "test-wf", {
            "name": "test-wf",
            "provides": {
                "workflows": [{"type": "cron", "class": "src.wf.MyCronWorkflow"}],
            },
        })
        src = mod_dir / "src"
        src.mkdir()
        (src / "wf.py").write_text(
            "from agento.framework.workflows.base import Workflow\n"
            "class MyCronWorkflow(Workflow):\n"
            "    def build_prompt(self, channel, ref, **kw): return 'test'\n"
        )

        bootstrap(str(tmp_path))

        assert _WORKFLOW_MAP[AgentType.CRON].__name__ == "MyCronWorkflow"
        # BlankWorkflow always registered
        assert AgentType.BLANK in _WORKFLOW_MAP

    def test_bad_module_does_not_crash(self, tmp_path: Path):
        _write_module(tmp_path, "bad", {
            "name": "bad",
            "provides": {
                "channels": [{"name": "x", "class": "src.nope.Missing"}],
            },
        })

        # Should not raise — logs error and continues
        result = bootstrap(str(tmp_path))
        assert len(result) == 1

    def test_bootstrap_registers_blank_workflow(self, tmp_path: Path):
        bootstrap(str(tmp_path))
        assert AgentType.BLANK in _WORKFLOW_MAP

    def test_removing_module_removes_capabilities(self, tmp_path: Path):
        """Removing a module directory cleanly removes its capabilities."""
        import shutil

        mod_dir = _write_module(tmp_path, "ephemeral", {
            "name": "ephemeral",
            "provides": {
                "channels": [{"name": "ephemeral", "class": "src.ch.EphemeralChannel"}],
            },
        })
        src = mod_dir / "src"
        src.mkdir()
        (src / "ch.py").write_text(
            "class EphemeralChannel:\n"
            "    @property\n"
            "    def name(self): return 'ephemeral'\n"
            "    def get_prompt_fragments(self, ref): pass\n"
            "    def get_followup_fragments(self, ref, instr): pass\n"
        )

        bootstrap(str(tmp_path))
        assert channel_registry.get_channel("ephemeral").name == "ephemeral"

        # Remove the module and re-bootstrap
        shutil.rmtree(mod_dir)
        bootstrap(str(tmp_path))

        with pytest.raises(ValueError, match="Unknown channel"):
            channel_registry.get_channel("ephemeral")


class TestBootstrapObservers:
    def test_loads_observer_from_events_json(self, tmp_path: Path):
        mod_dir = _write_module(tmp_path, "obs-mod", {"name": "obs-mod"})
        src = mod_dir / "src"
        src.mkdir()
        (src / "obs.py").write_text(
            "class MyObserver:\n"
            "    def execute(self, event): pass\n"
        )
        (mod_dir / "events.json").write_text(json.dumps({
            "job_fail_after": [{"name": "obs_mod_failed", "class": "src.obs.MyObserver"}],
        }))

        bootstrap(str(tmp_path))
        assert get_event_manager().observer_count("job_fail_after") == 1

    def test_bad_observer_does_not_crash(self, tmp_path: Path):
        mod_dir = _write_module(tmp_path, "bad-obs", {"name": "bad-obs"})
        (mod_dir / "events.json").write_text(json.dumps({
            "job_fail_after": [{"name": "bad", "class": "src.nope.Missing"}],
        }))

        result = bootstrap(str(tmp_path))
        assert len(result) == 1
        assert get_event_manager().observer_count("job_fail_after") == 0

    def test_lifecycle_events_dispatched(self, tmp_path: Path):
        """module_register and module_ready events are dispatched during bootstrap."""
        calls: list[str] = []

        class RegisterObs:
            def execute(self, event):
                calls.append(f"register:{event.name}")

        class ReadyObs:
            def execute(self, event):
                calls.append(f"ready:{event.name}")

        mod_dir = _write_module(tmp_path, "lc-mod", {"name": "lc-mod"})
        src = mod_dir / "src"
        src.mkdir()
        (src / "obs.py").write_text(
            "class RegisterObs:\n"
            "    def execute(self, event):\n"
            "        import tests.unit.framework.test_bootstrap as tb\n"
            "        tb._lifecycle_calls.append(f'register:{event.name}')\n"
            "\n"
            "class ReadyObs:\n"
            "    def execute(self, event):\n"
            "        import tests.unit.framework.test_bootstrap as tb\n"
            "        tb._lifecycle_calls.append(f'ready:{event.name}')\n"
        )
        (mod_dir / "events.json").write_text(json.dumps({
            "module_register_before": [{"name": "lc_register", "class": "src.obs.RegisterObs"}],
            "module_ready_after": [{"name": "lc_ready", "class": "src.obs.ReadyObs"}],
        }))

        import tests.unit.framework.test_bootstrap as tb
        tb._lifecycle_calls = []

        bootstrap(str(tmp_path))

        assert "register:lc-mod" in tb._lifecycle_calls
        assert "ready:lc-mod" in tb._lifecycle_calls

    def test_bootstrap_clears_event_manager(self, tmp_path: Path):
        from agento.framework.event_manager import ObserverEntry

        class Dummy:
            def execute(self, event): pass

        get_event_manager().register("test_ev", ObserverEntry(name="d", observer_class=Dummy))
        assert get_event_manager().observer_count("test_ev") == 1

        bootstrap(str(tmp_path))
        assert get_event_manager().observer_count("test_ev") == 0

    def test_manifests_stored_after_bootstrap(self, tmp_path: Path):
        _write_module(tmp_path, "m1", {"name": "m1"})
        bootstrap(str(tmp_path))
        manifests = get_manifests()
        assert len(manifests) == 1
        assert manifests[0].name == "m1"


class TestBootstrapRegexIdentityTypes:
    """C3: regex identity types are module-owned — declared via di.json `regex_identity_types`,
    populated into the framework registry at bootstrap, dropped when the module is disabled."""

    def test_di_json_populates_registry(self, tmp_path: Path):
        _write_module(tmp_path, "regex-mod", {
            "name": "regex-mod",
            "provides": {"regex_identity_types": ["test_sender"]},
        })
        bootstrap(str(tmp_path))
        assert is_regex_identity_type("test_sender")

    def test_absent_when_module_disabled(self, tmp_path: Path, monkeypatch):
        _write_module(tmp_path, "regex-mod", {
            "name": "regex-mod",
            "provides": {"regex_identity_types": ["test_sender"]},
        })
        monkeypatch.setattr(
            "agento.framework.module_status.read_module_status",
            lambda *a, **k: {"regex-mod": False},
        )
        bootstrap(str(tmp_path))
        assert not is_regex_identity_type("test_sender")

    def test_rebootstrap_clears_and_repopulates(self, tmp_path: Path):
        import shutil

        m1 = _write_module(tmp_path, "regex-mod", {
            "name": "regex-mod",
            "provides": {"regex_identity_types": ["test_sender"]},
        })
        bootstrap(str(tmp_path))
        assert is_regex_identity_type("test_sender")

        shutil.rmtree(m1)
        _write_module(tmp_path, "other-mod", {
            "name": "other-mod",
            "provides": {"regex_identity_types": ["other_sender"]},
        })
        bootstrap(str(tmp_path))
        assert not is_regex_identity_type("test_sender")  # cleared
        assert is_regex_identity_type("other_sender")     # repopulated

    def test_malformed_entry_skipped_valid_registered(self, tmp_path: Path):
        # The loader applies the canonical form ^[a-z][a-z0-9_]{0,31}$ as a fail-safe backstop:
        # a malformed entry is skipped (logged, never raised) and valid ones still register.
        _write_module(tmp_path, "regex-mod", {
            "name": "regex-mod",
            "provides": {"regex_identity_types": ["good_type", "Bad Type", "", "x" * 40, 123]},
        })
        bootstrap(str(tmp_path))  # must not raise despite the malformed entries
        assert is_regex_identity_type("good_type")
        assert not is_regex_identity_type("Bad Type")
        assert not is_regex_identity_type("")
        assert not is_regex_identity_type("x" * 40)

    @pytest.mark.parametrize("declared", ["abc", {"a": 1}, 123, None])
    def test_malformed_container_does_not_crash_registers_nothing(self, tmp_path: Path, declared):
        # The loader guards the CONTAINER before iterating: a non-list `regex_identity_types` is
        # logged and skipped, never raised — so it can't crash bootstrap (which also runs on every
        # consumer hot-reload, past setup-time validation). Critically, a string is NOT iterated
        # char-by-char into 'a','b','c'.
        _write_module(tmp_path, "bad-container-mod", {
            "name": "bad-container-mod",
            "provides": {"regex_identity_types": declared},
        })
        result = bootstrap(str(tmp_path))  # must not raise (TypeError on None/int, char-iter on str)
        assert result is not None
        for t in ("abc", "a", "b", "c"):
            assert not is_regex_identity_type(t)


# Used by test_lifecycle_events_dispatched
_lifecycle_calls: list[str] = []
