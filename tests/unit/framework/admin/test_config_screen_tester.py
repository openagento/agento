"""ConfigScreen 't' action — guards, threading seam, and result formatting."""
from __future__ import annotations

from agento.framework.admin.data import ResolvedField
from agento.framework.admin.screens.config import ConfigScreen, _format_result
from agento.framework.config_test import ERROR, FAIL, NOT_CONFIGURED, OK, TestResult


def _field(**kw):
    base = dict(
        path="m/smtp_pass", field_name="smtp_pass", value="h", display_value="h",
        source="db", field_type="string", label="Host", obscure=False, tester="smtp",
    )
    base.update(kw)
    return ResolvedField(**base)


def test_t_is_bound_in_the_footer():
    keys = {b.key: b for b in ConfigScreen.BINDINGS}
    assert "t" in keys
    assert "Test" in keys["t"].description


def test_the_binding_maps_to_the_action():
    assert {b.key: b.action for b in ConfigScreen.BINDINGS}["t"] == "test_field"


def test_format_result_ok():
    label, severity = _format_result(TestResult(OK, "connected to mail:587"))
    assert label.startswith("OK")
    assert severity == "information"


def test_format_result_fail_includes_the_code():
    label, severity = _format_result(
        TestResult(FAIL, "535 5.7.8 authentication failed", code="AUTH_FAILED")
    )
    assert "FAIL" in label
    assert "AUTH_FAILED" in label
    assert severity == "error"


def test_format_result_error_is_error_severity():
    _, severity = _format_result(TestResult(ERROR, "toolbox unreachable", code="TOOLBOX_UNREACHABLE"))
    assert severity == "error"


def test_format_result_not_configured_is_a_warning():
    label, severity = _format_result(TestResult(NOT_CONFIGURED, "no host configured"))
    assert "NOT_CONFIGURED" in label
    assert severity == "warning"


def test_a_field_without_a_tester_is_refused(monkeypatch):
    screen = ConfigScreen.__new__(ConfigScreen)   # no Textual app needed
    notes = []
    monkeypatch.setattr(
        type(screen), "notify",
        lambda self, msg, severity="information": notes.append((msg, severity)),
        raising=False,
    )
    monkeypatch.setattr(
        type(screen), "_get_selected_field", lambda self: _field(tester=""),
        raising=False,
    )
    ran = []
    monkeypatch.setattr(
        type(screen), "_do_test",
        lambda self, field, scope=None, scope_id=None: ran.append(field), raising=False
    )
    screen.action_test_field()
    assert ran == []
    assert "no test" in notes[0][0].lower()


def test_no_field_selected_is_refused(monkeypatch):
    screen = ConfigScreen.__new__(ConfigScreen)
    notes = []
    monkeypatch.setattr(
        type(screen), "notify",
        lambda self, msg, severity="information": notes.append((msg, severity)),
        raising=False,
    )
    monkeypatch.setattr(
        type(screen), "_get_selected_field", lambda self: None, raising=False
    )
    ran = []
    monkeypatch.setattr(
        type(screen), "_do_test",
        lambda self, field, scope=None, scope_id=None: ran.append(field), raising=False
    )
    screen.action_test_field()
    assert ran == []
    assert notes


def test_a_field_with_a_tester_dispatches_the_worker(monkeypatch):
    screen = ConfigScreen.__new__(ConfigScreen)
    monkeypatch.setattr(
        type(screen), "notify", lambda self, *a, **k: None, raising=False
    )
    field = _field()
    monkeypatch.setattr(
        type(screen), "_get_selected_field", lambda self: field, raising=False
    )
    screen._current_scope, screen._current_scope_id = "default", 0
    ran = []
    monkeypatch.setattr(
        type(screen), "_do_test",
        lambda self, f, scope=None, scope_id=None: ran.append(f), raising=False
    )
    screen.action_test_field()
    assert ran == [field]


def test_the_worker_thread_never_touches_a_textual_object():
    """Textual: a method call on a Textual object from another thread has
    unpredictable results. `_do_test` runs in a thread, so every UI touch must go
    through `call_from_thread` — the DOM query included. Structural check: no
    `query_one`, no `self.notify`, no widget attribute access in that body."""
    import ast
    import inspect

    from agento.framework.admin.screens import config as mod

    source = inspect.getsource(mod)
    fn = next(
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.FunctionDef) and n.name == "_do_test"
    )
    direct = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ]
    assert direct == [], [ast.unparse(d) for d in direct]


class TestADelayedResultIsBoundToItsFieldAndScope:
    """A probe takes seconds; the operator can select another field or switch
    scope while it runs. The verdict belongs to the (field, scope) it measured."""

    def _screen(self, monkeypatch, selected, scope=("default", 0)):
        from agento.framework.admin.widgets.field_detail import FieldDetailPanel

        screen = ConfigScreen.__new__(ConfigScreen)
        screen._current_scope, screen._current_scope_id = scope
        panel = FieldDetailPanel.__new__(FieldDetailPanel)
        panel._results, panel._scope = {}, scope
        rendered = []
        monkeypatch.setattr(type(panel), "update_field", lambda self, f: rendered.append(f), raising=False)
        monkeypatch.setattr(type(screen), "query_one", lambda self, _t: panel, raising=False)
        monkeypatch.setattr(type(screen), "notify", lambda self, *a, **k: None, raising=False)
        monkeypatch.setattr(type(screen), "_get_selected_field", lambda self: selected, raising=False)
        return screen, panel, rendered

    def test_it_renders_when_the_selection_and_scope_still_match(self, monkeypatch):
        field = _field()
        screen, panel, rendered = self._screen(monkeypatch, field)
        screen._show_test_result(field, "OK — connected", "information", "default", 0)
        assert rendered == [field]
        assert panel._results[("m/smtp_pass", "default", 0)] == "OK — connected"

    def test_it_does_not_overwrite_a_different_field(self, monkeypatch):
        probed, now_selected = _field(), _field(path="m/other", field_name="other")
        screen, _panel, rendered = self._screen(monkeypatch, now_selected)
        screen._show_test_result(probed, "OK — connected", "information", "default", 0)
        assert rendered == []

    def test_it_does_not_render_a_result_from_another_scope(self, monkeypatch):
        field = _field()
        screen, panel, rendered = self._screen(monkeypatch, field, scope=("agent_view", 7))
        screen._show_test_result(field, "OK — connected", "information", "default", 0)
        assert rendered == []
        # …and it is filed under the scope it was measured at, not the current one.
        assert ("m/smtp_pass", "default", 0) in panel._results
        assert ("m/smtp_pass", "agent_view", 7) not in panel._results


def test_the_panel_only_shows_a_result_recorded_for_the_current_scope():
    from agento.framework.admin.widgets.field_detail import FieldDetailPanel

    panel = FieldDetailPanel.__new__(FieldDetailPanel)
    panel._results, panel._scope = {}, ("default", 0)
    panel.set_test_result("m/smtp_pass", "FAIL — AUTH_FAILED", "default", 0)
    lines = []
    panel.update = lambda text: lines.append(text)
    panel.update_field(_field())
    assert "FAIL — AUTH_FAILED" in lines[0]

    panel.set_scope("agent_view", 7)
    lines.clear()
    panel.update_field(_field())
    assert "FAIL — AUTH_FAILED" not in lines[0]
