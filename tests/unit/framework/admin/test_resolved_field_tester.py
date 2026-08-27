"""get_resolved_fields must carry a tester display label through to the UI."""
from __future__ import annotations

import json

import pytest

from agento.framework.admin.data import ModuleSchema, ResolvedField, get_resolved_fields
from agento.framework.config_test.manifest import tester_label as _tester_label


def test_resolved_field_defaults_to_no_tester():
    f = ResolvedField(
        path="m/x", field_name="x", value=None, display_value="", source="none",
        field_type="string", label="X", obscure=False,
    )
    assert f.tester == ""


def test_tester_label_covers_all_three_declaration_forms(tmp_path):
    """One normalizer for the UI and the runner: `tester_label` is a wrapper over
    `_ref_from`, so a field the TUI offers `t` on is a field the runner accepts."""
    assert _tester_label("m", tmp_path, {"tester": {"kind": "smtp", "host": "h"}}) == "smtp"
    assert _tester_label("m", tmp_path, {"tester": {"kind": "http", "url": "u"}}) == "http"
    assert _tester_label("m", tmp_path, {"tester": "graph_credentials"}) == "graph_credentials"
    # The explicit form of that sugar labels the same way: the operator needs to
    # know WHICH probe runs, not which arm of the framework runs it.
    assert _tester_label(
        "m", tmp_path, {"tester": {"kind": "toolbox", "name": "graph_credentials"}}
    ) == "graph_credentials"
    assert _tester_label("m", tmp_path, {"tester": {"kind": "toolbox"}}) == ""
    assert _tester_label(
        "m", tmp_path, {"tester": {"kind": "local", "class": "src.t.T"}}
    ) == "local"


def test_tester_label_is_empty_for_junk(tmp_path):
    """A malformed declaration must not light up the `t` binding. `module:validate`
    (Task 3) rejects these at the manifest gate; if one reaches the TUI anyway,
    offering a test that cannot run is worse than offering none."""
    assert _tester_label("m", tmp_path, {}) == ""
    assert _tester_label("m", tmp_path, {"tester": None}) == ""
    assert _tester_label("m", tmp_path, {"tester": 7}) == ""
    assert _tester_label("m", tmp_path, {"tester": {"kind": "local"}}) == ""


def test_an_unknown_kind_still_gets_a_label(tmp_path):
    """`_ref_from` (Task 2) forwards every non-`local` kind to the toolbox unread —
    the toolbox owns the kind table, not the framework. So an unknown kind labels
    itself and the test would come back ERROR [UNKNOWN_KIND]. This is not a hole:
    `module:validate` (Task 3) rejects an unknown kind before it can ever ship."""
    assert _tester_label("m", tmp_path, {"tester": {"kind": "nonsense"}}) == "nonsense"


@pytest.fixture
def module_with_tester(tmp_path, monkeypatch):
    schema = ModuleSchema(
        name="m",
        fields={
            "smtp_pass": {
                "type": "obscure", "label": "P",
                "tester": {"kind": "smtp", "host": "{m/smtp_host}"},
            },
            "plain": {"type": "string", "label": "P"},
        },
        tools={},
        module_path=tmp_path,
    )
    (tmp_path / "config.json").write_text(json.dumps({}))
    monkeypatch.setattr(
        "agento.framework.admin.data.get_module_schemas", lambda: [schema]
    )
    return schema


def test_the_label_reaches_resolved_field(module_with_tester):
    fields = {f.field_name: f for f in get_resolved_fields(None, "m")}
    assert fields["smtp_pass"].tester == "smtp"
    assert fields["plain"].tester == ""
