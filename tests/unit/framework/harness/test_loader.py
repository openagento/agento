"""The harness-only loader (framework/harness/loader.py): web and the run worker get the
harness registry without bootstrap(): no observer, no event, and a module enabled after
start is seen with no restart."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from agento.framework import module_status
from agento.framework.event_manager import get_event_manager
from agento.framework.harness import clear, find_harness
from agento.framework.harness.loader import harness_reloader

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "modules"


@pytest.fixture
def modules(tmp_path, monkeypatch):
    core = tmp_path / "core"
    shutil.copytree(FIXTURES / "fake_harness", core / "fake_harness")
    etc = tmp_path / "etc"
    etc.mkdir()
    monkeypatch.setattr(module_status, "_DOCKER_PATH", etc / "modules.json")
    clear()
    yield core, etc / "modules.json"
    clear()


def _write(path: Path, status: dict, mtime: int) -> None:
    path.write_text(json.dumps(status))
    os.utime(path, (mtime, mtime))


def test_a_module_enabled_after_start_is_registered_without_a_restart(modules, tmp_path):
    core, status = modules
    _write(status, {"fake_harness": False}, 1_000)
    reload = harness_reloader(str(core), str(tmp_path / "no-user-dir"))

    reload()
    assert find_harness("fake") is None

    _write(status, {"fake_harness": True}, 2_000)  # module:enable
    reload()
    assert find_harness("fake") is not None

    _write(status, {"fake_harness": False}, 3_000)  # module:disable
    reload()
    assert find_harness("fake") is None


def test_an_unchanged_modules_json_loads_nothing_again(modules, tmp_path, monkeypatch):
    core, status = modules
    _write(status, {}, 1_000)
    reload = harness_reloader(str(core), str(tmp_path / "no-user-dir"))
    reload()
    calls = []
    monkeypatch.setattr("agento.framework.harness.loader.load_harnesses", calls.append)

    reload()

    assert calls == [] and find_harness("fake") is not None


def test_it_loads_no_observer_and_dispatches_no_event(modules, tmp_path, monkeypatch):
    """EVT-3: observers are module code; the loader runs none of them."""
    core, _ = modules
    em = get_event_manager()
    monkeypatch.setattr(em, "dispatch", lambda *a, **k: pytest.fail("dispatched an event"))
    monkeypatch.setattr(em, "register", lambda *a, **k: pytest.fail("loaded an observer"))

    harness_reloader(str(core), str(tmp_path / "no-user-dir"))()

    assert find_harness("fake") is not None
