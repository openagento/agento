"""The execution seams reached through a real `bootstrap()` (PRD E3-E5 §5.1).

Declared in `di.json`, loaded by `_load_execution_hooks`, cleared on every reload. Tasks
that add the other three implementations add one line each to the first test here; the file
grows with the plan rather than asserting a future that does not exist yet.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.framework import execution_hooks
from agento.framework.bootstrap import _load_execution_hooks
from agento.framework.execution_hooks import SEAMS, clear
from agento.framework.module_loader import ModuleManifest
from agento.modules.conversation.src.deltas import ConversationDeltaSink
from agento.modules.conversation.src.finalizer import ConversationFinalizer
from agento.modules.conversation.src.hooks import (
    ConversationExecutionIds,
    ConversationResumeSessions,
)

from .conftest import bootstrap_for_tests


@pytest.fixture(autouse=True)
def _restore():
    yield
    clear()
    bootstrap_for_tests()


def _registered() -> dict[str, object]:
    return {name: slot.get() for name, slot in SEAMS.items()}


def test_bootstrap_registers_the_modules_declared_hooks():
    bootstrap_for_tests()

    live = _registered()
    assert isinstance(live["execution_id_provider"], ConversationExecutionIds)
    assert isinstance(live["resume_session_resolver"], ConversationResumeSessions)
    assert isinstance(live["execution_finalizer"], ConversationFinalizer)
    assert isinstance(live["execution_delta_sink"], ConversationDeltaSink)


def test_a_second_bootstrap_leaves_one_not_two():
    """The consumer re-bootstraps every idle poll tick."""
    bootstrap_for_tests()
    first = SEAMS["execution_id_provider"].get()

    bootstrap_for_tests()

    second = SEAMS["execution_id_provider"].get()
    assert second is not first          # a fresh instance, not an appended one
    assert SEAMS["execution_id_provider"].module() == "conversation"


def test_with_the_module_disabled_every_seam_is_empty(monkeypatch):
    from agento.framework import bootstrap as bootstrap_module

    clear()
    monkeypatch.setattr(
        "agento.framework.module_status.read_module_status",
        lambda *a, **k: {"conversation": False},
    )
    bootstrap_module.bootstrap(quiet=True)

    assert list(_registered().values()) == [None, None, None, None]


def _manifest(tmp_path, hooks: dict, body: str) -> ModuleManifest:
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "src" / "hooks.py").write_text(body)
    (tmp_path / "di.json").write_text(json.dumps({"execution_hooks": hooks}))
    return ModuleManifest(name="fixture", path=tmp_path, version="1.0.0",
                          description="test fixture", provides={"execution_hooks": hooks})


def test_a_class_that_does_not_satisfy_its_protocol_is_a_load_error(tmp_path):
    """Loud, not swallowed: a silently missing provider is a run nothing records."""
    clear()
    manifest = _manifest(tmp_path, {"execution_id_provider": "src.hooks.Wrong"},
                         "class Wrong:\n    def not_mint(self):\n        return None\n")

    with pytest.raises(TypeError) as exc:
        _load_execution_hooks(manifest)

    assert "does not implement execution_id_provider" in str(exc.value)


def test_an_unknown_seam_name_is_a_load_error(tmp_path):
    clear()
    manifest = _manifest(tmp_path, {"execution_typo": "src.hooks.Wrong"},
                         "class Wrong:\n    pass\n")

    with pytest.raises(ValueError) as exc:
        _load_execution_hooks(manifest)

    assert "unknown execution hook 'execution_typo'" in str(exc.value)


def test_module_validate_refuses_a_second_provider():
    from agento.framework.module_validator import validate_execution_hooks

    errors = validate_execution_hooks([
        ("conversation", {"execution_id_provider": "src.hooks.A"}),
        ("intruder", {"execution_id_provider": "src.hooks.B"}),
    ])

    assert "already provided by module 'conversation'" in errors["intruder"][0]
    assert "conversation" not in errors


def test_module_validate_allows_different_seams_in_different_modules():
    from agento.framework.module_validator import validate_execution_hooks

    assert validate_execution_hooks([
        ("conversation", {"execution_id_provider": "src.hooks.A"}),
        ("other", {"execution_delta_sink": "src.hooks.B"}),
    ]) == {}


def test_the_shipped_manifest_declares_its_hooks():
    """Through the file bootstrap reads, so a deleted di.json entry fails here."""
    from agento.framework.bootstrap import CORE_MODULES_DIR

    di = json.loads((Path(CORE_MODULES_DIR) / "conversation" / "di.json").read_text())

    assert di["execution_hooks"] == {
        "execution_id_provider": "src.hooks.ConversationExecutionIds",
        "resume_session_resolver": "src.hooks.ConversationResumeSessions",
        "execution_finalizer": "src.finalizer.ConversationFinalizer",
        "execution_delta_sink": "src.deltas.ConversationDeltaSink"}


def test_the_framework_module_exposes_exactly_four_seams():
    assert sorted(execution_hooks.SEAMS) == [
        "execution_delta_sink", "execution_finalizer",
        "execution_id_provider", "resume_session_resolver",
    ]
