"""The run's lethal-trifecta assessment (framework/tool_risk.py, Task 6)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agento.framework import tool_risk as tr
from agento.framework.module_loader import ModuleManifest
from agento.framework.test_run import LabelSnapshot, SnapshotTooLarge
from agento.framework.tool_risk import AssessTool, assess, assess_run, with_direct

PD, UC, EC = "private-data", "untrusted-content", "external-communication"
STUB = "stub-in-test"


def _t(name: str, *labels: str, module: str = "m", defaults: tuple[str, ...] = (),
       conditional: bool = False) -> AssessTool:
    return AssessTool(id=f"{module}/{name}", name=name, default_labels=frozenset(defaults or labels),
                      labels=frozenset(labels), conditional=conditional)


def _v2(**over) -> dict:
    policy = {"v": 2, "stub": [], "real": [], "stub_labels": [], "unstub": [], "known": []}
    policy.update(over)
    return policy


THREE = [_t("read", PD), _t("fetch", UC), _t("send", EC)]


@pytest.mark.parametrize(("tools", "available", "policy", "expected"), [
    pytest.param(THREE, {"read", "fetch", "send"}, None, True, id="three-tools"),
    pytest.param(THREE, {"read", "fetch"}, None, False, id="one-missing"),
    pytest.param([_t("all", PD, UC, EC)], {"all"}, None, True, id="all-on-one-tool"),
    # `send` is blocked by its `requires` master: the gate leaves it out of `available`.
    pytest.param(THREE, {"read", "fetch"}, None, False, id="blocked-by-requires"),
    pytest.param(THREE, {"read", "fetch", "send"},
                 _v2(stub=["send"], known=["m/read", "m/fetch", "m/send"]), False, id="stubbed-excluded"),
])
def test_trifecta_cases(tools, available, policy, expected):
    risk = assess(tools, available, policy, modules_changed=False)
    assert risk["trifecta"] is expected
    assert risk["tool_coverage"] == "complete"


def test_categories_name_the_counted_tools():
    risk = assess(THREE, {"read", "fetch", "send"}, None, modules_changed=False)
    assert risk["categories"] == {PD: ["read"], UC: ["fetch"], EC: ["send"]}


def test_pending_until_recorded():
    risk = assess(THREE, {"read", "fetch", "send"}, None, modules_changed=False)
    assert (risk["direct"], risk["coverage"], risk["uncovered"]) == ("pending", "incomplete", [])


def test_pending_to_recorded_complete():
    risk = with_direct(assess(THREE, set(), None, modules_changed=False), [])
    assert (risk["direct"], risk["coverage"], risk["trifecta"]) == ("recorded", "complete", False)


def test_incomplete_when_direct_server():
    risk = with_direct(assess(THREE, set(), None, modules_changed=False), ["github", "toolbox"])
    assert (risk["coverage"], risk["uncovered"]) == ("incomplete", ["github", "toolbox"])
    assert risk["tool_coverage"] == "complete"


def test_incomplete_when_harness_unknown():
    risk = with_direct(assess(THREE, set(), None, modules_changed=False), None)
    assert (risk["direct"], risk["coverage"], risk["uncovered"]) == ("recorded", "incomplete", [])


def test_incomplete_still_flags_trifecta():
    risk = with_direct(assess(THREE, {"read", "fetch", "send"}, None, modules_changed=True), ["x"])
    assert risk["trifecta"] is True
    assert (risk["tool_coverage"], risk["coverage"], risk["notes"]) == (
        "incomplete", "incomplete", ["modules_changed"])


def test_conditional_only_category_is_possible():
    tools = [_t("read", PD), _t("fetch", UC), _t("gh_comment", EC, conditional=True)]
    risk = assess(tools, {"read", "fetch", "gh_comment"}, None, modules_changed=False)
    assert risk["trifecta"] == "possible"
    assert (risk["tool_coverage"], risk["notes"]) == ("incomplete", ["conditional_tools"])
    assert with_direct(risk, [])["coverage"] == "incomplete"


def test_unconditional_cover_wins_over_conditional():
    tools = [*THREE, _t("gh_comment", EC, conditional=True)]
    risk = assess(tools, {"read", "fetch", "send", "gh_comment"}, None, modules_changed=False)
    assert risk["trifecta"] is True
    assert risk["tool_coverage"] == "complete"


def test_inherited_policy_uses_dispatcher_rule():
    """Counted exactly as `is_stubbed` decides: a default `stub-in-test` tool outside `stub`
    is refused (nothing), the same tool in `unstub` runs (counted), a tool outside `known`
    is refused, a name in `stub` is stubbed."""
    tools = [_t("read", PD, defaults=(PD, STUB)), _t("fetch", UC, defaults=(UC, STUB), module="n"),
             _t("send", EC), _t("new_tool", EC, module="late")]
    policy = _v2(stub=["send"], unstub=["n/fetch"], known=["m/read", "n/fetch", "m/send"])
    risk = assess(tools, {"read", "fetch", "send", "new_tool"}, policy, modules_changed=False)
    assert risk["categories"] == {PD: [], UC: ["fetch"], EC: []}


def test_lists_bounded():
    tools = [_t(f"t{i:03}", PD) for i in range(60)]
    risk = assess(tools, {t.name for t in tools}, None, modules_changed=False)
    assert len(risk["categories"][PD]) == 51
    assert risk["categories"][PD][-1] == "…+10"
    uncovered = with_direct(risk, [f"s{i:02}" for i in range(25)])["uncovered"]
    assert (len(uncovered), uncovered[-1]) == (21, "…+5")


def test_with_direct_keeps_error():
    risk = with_direct(tr.error_assessment(), [])
    assert (risk["coverage"], risk["tool_coverage"], risk["direct"]) == ("error", "error", "recorded")


def test_error_object_shape():
    assert tr.error_assessment() == {
        "v": 1, "trifecta": False, "tool_coverage": "error", "coverage": "error", "direct": "pending",
        "categories": {}, "uncovered": [], "notes": []}


# --- assess_run: manifests + snapshot + the view's gate ---------------------------------

def _manifest(tmp_path: Path, name: str, tools: list[dict]) -> ModuleManifest:
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    (path / "module.json").write_text(json.dumps({"name": name, "tools": tools}))
    return ModuleManifest(name=name, version="1", description="", path=path, tools=tools)


@pytest.fixture
def in_tree():
    with patch("agento.framework.test_run.is_in_tree", return_value=True):
        yield


def _run(manifests, snapshot, *, available=None, policy=None, view=3):
    def gate(_conn, _view, names):
        return set(names) if available is None else set(names) & available

    with patch("agento.framework.tool_enablement.effective_tools_for_view", side_effect=gate), \
            patch.object(tr, "modules_changed_since_bootstrap", return_value=False):
        return assess_run(object(), view, policy, snapshot, manifests)


def _snapshot(adds=None, removes=None) -> LabelSnapshot:
    labels = {PD, UC, EC, STUB}
    return LabelSnapshot(labels=labels, adds=adds or {}, removes=removes or {})


def test_assess_run_counts_effective_labels(tmp_path, in_tree):
    manifests = [_manifest(tmp_path, "m", [
        {"name": "read", "labels": [PD, UC]}, {"name": "send"}])]
    risk = _run(manifests, _snapshot(adds={EC: {"m/send"}}))
    assert risk["trifecta"] is True
    assert risk["categories"][EC] == ["send"]


def test_assess_run_follows_the_view_gate(tmp_path, in_tree):
    manifests = [_manifest(tmp_path, "m", [{"name": "read", "labels": [PD, UC]}, {"name": "send", "labels": [EC]}])]
    assert _run(manifests, _snapshot(), available={"read"})["trifecta"] is False


def test_a_run_without_an_agent_view_has_no_tools(tmp_path, in_tree):
    manifests = [_manifest(tmp_path, "m", [{"name": "all", "labels": [PD, UC, EC]}])]
    assert _run(manifests, _snapshot(), view=None)["trifecta"] is False


def test_conditional_from_manifest(tmp_path, in_tree):
    manifests = [_manifest(tmp_path, "m", [
        {"name": "read", "labels": [PD, UC]},
        {"name": "gh_comment", "labels": [EC], "availability": "conditional"}])]
    assert _run(manifests, _snapshot())["trifecta"] == "possible"


def test_child_after_admin_adds_stub_in_test_counts_tool(tmp_path, in_tree):
    """The parent resolved before the admin added `stub-in-test` to `send`: the child's
    inherited policy does not stub it and the toolbox runs it (its default has no
    `stub-in-test`), so the assessment counts it."""
    manifests = [_manifest(tmp_path, "m", [{"name": "read", "labels": [PD, UC]}, {"name": "send", "labels": [EC]}])]
    inherited = _v2(known=["m/read", "m/send"])
    risk = _run(manifests, _snapshot(adds={STUB: {"m/send"}}), policy=inherited)
    assert risk["trifecta"] is True


def test_snapshot_overflow_is_error(tmp_path):
    risk = assess_run(object(), 3, None, None, [], error=SnapshotTooLarge("over"))
    assert (risk["tool_coverage"], risk["coverage"], risk["notes"]) == ("error", "error", ["snapshot_too_large"])


def test_error_is_recorded_not_raised(tmp_path, in_tree, caplog):
    manifests = [_manifest(tmp_path, "m", [{"name": "read", "labels": [PD]}])]
    with patch("agento.framework.tool_enablement.effective_tools_for_view",
               side_effect=RuntimeError("secret dsn mysql://u:p@h")):
        risk = assess_run(object(), 3, None, _snapshot(), manifests)
    assert risk == tr.error_assessment()
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text


# --- modules_changed_since_bootstrap ---------------------------------------------------

@pytest.mark.parametrize("touched", ["module.json", "config.json", "modules.json"])
def test_modules_changed_is_incomplete(tmp_path, touched, monkeypatch):
    m = _manifest(tmp_path, "m", [])
    (m.path / "config.json").write_text("{}")
    status = tmp_path / "modules.json"
    status.write_text("{}")
    started = max(p.stat().st_mtime_ns for p in (m.path / "module.json", m.path / "config.json", status))
    monkeypatch.setattr(tr, "_CHANGED_AT", [])
    with patch("agento.framework.bootstrap.bootstrapped_at", return_value=started + 1), \
            patch("agento.framework.bootstrap.get_manifests", return_value=[m]), \
            patch("agento.framework.module_status._resolve_path", return_value=status):
        assert tr.modules_changed_since_bootstrap() is False
        target = status if touched == "modules.json" else m.path / touched
        future = (started + 10**10) / 1e9
        os.utime(target, (future, future))
        assert tr.modules_changed_since_bootstrap() is True
        risk = assess(THREE, set(), None, modules_changed=tr.modules_changed_since_bootstrap())
    assert (risk["tool_coverage"], risk["notes"]) == ("incomplete", ["modules_changed"])


def test_not_bootstrapped_is_not_changed():
    with patch("agento.framework.bootstrap.bootstrapped_at", return_value=None):
        assert tr.modules_changed_since_bootstrap() is False
