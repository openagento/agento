"""A harness's native-config passthrough is shown in the agent_view node, per harness.

``claude/settings`` / ``codex/config`` / ``pi/settings`` are set per agent_view, so an
operator looks for them beside the harness selector — not on a module node for a harness
this view does not use. The field marks itself with ``harness_option`` and admin borrows
it into the agent_view listing, hiding the ones that belong to other harnesses.

The path stays ``<harness module>/<field>``: that is what ``runtime_config_fields``
resolves, and moving the field into ``agent_view`` would put harness names into a core
module. Only the DISPLAY moves.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.admin.data import ModuleSchema, get_resolved_fields
from agento.framework.harness import is_harness_option_hidden, modules_declaring
from agento.framework.scoped_config import Scope

SETTINGS_FIELD = {"type": "textarea", "harness_option": True, "label": "Claude settings"}
REPO = Path(__file__).resolve().parents[4]


class TestVisibilityAgainstTheShippedDeclarations:
    """Reads the real manifests off disk — the same path admin uses."""

    @pytest.mark.parametrize("harness", ["claude", "codex", "pi"])
    def test_a_harness_is_owned_by_the_module_that_declares_it(self, harness):
        assert modules_declaring(harness) == frozenset({harness})

    def test_visible_for_the_harness_that_owns_it(self):
        assert not is_harness_option_hidden(SETTINGS_FIELD, module="claude", harness="claude")

    def test_hidden_for_another_harness(self):
        assert is_harness_option_hidden(SETTINGS_FIELD, module="claude", harness="codex")

    @pytest.mark.parametrize("harness", [None, ""])
    def test_an_unset_harness_leaves_every_passthrough_visible(self, harness):
        """Hiding a field the operator still needs to set is the worse failure, and at the
        default scope there may legitimately be no harness chosen yet."""
        assert not is_harness_option_hidden(SETTINGS_FIELD, module="claude", harness=harness)

    def test_an_unresolvable_harness_leaves_the_field_visible(self):
        """A disabled module or a typo'd config value is MISSING INFORMATION, not a
        harness that owns nothing — the two must not collapse into "hide"."""
        assert not is_harness_option_hidden(
            SETTINGS_FIELD, module="claude", harness="not-a-real-harness",
        )

    def test_a_field_without_the_key_is_never_hidden(self):
        assert not is_harness_option_hidden({"type": "string"}, module="claude", harness="codex")


class TestTheShippedManifestsUseTheMechanism:
    """Guards the wiring itself: either side alone silently does nothing."""

    @pytest.mark.parametrize("module,field", [("claude", "settings"), ("codex", "config"), ("pi", "settings")])
    def test_each_passthrough_declares_itself(self, module, field):
        system = json.loads((REPO / f"src/agento/modules/{module}/system.json").read_text())
        assert system[field]["harness_option"] is True

    @pytest.mark.parametrize("module,field", [("claude", "settings"), ("codex", "config"), ("pi", "settings")])
    def test_the_same_field_is_the_runtime_config_field(self, module, field):
        """The borrowed field must be the one the harness may read while building a
        command — a marker on some OTHER field would surface a box that changes nothing."""
        di = json.loads((REPO / f"src/agento/modules/{module}/di.json").read_text())
        assert field in di["agent_harnesses"][0]["runtime_config_fields"]


class TestAdminBorrowsTheField:
    """End of the chain: the TUI must actually add (and drop) the row."""

    def _fields(self, module: str, resolved: dict):
        schemas = [
            ModuleSchema(name="agent_view", fields={"model": {"type": "string"}}, tools={}, module_path=None),
            ModuleSchema(name="claude", fields={"settings": dict(SETTINGS_FIELD)}, tools={}, module_path=None),
            ModuleSchema(
                name="codex",
                fields={"config": {"type": "textarea", "harness_option": True},
                        "private": {"type": "string"}},
                tools={},
                module_path=None,
            ),
        ]
        with patch(
            "agento.framework.admin.data.get_module_schemas", return_value=schemas
        ), patch(
            "agento.framework.admin.data.read_config_defaults", return_value={}
        ), patch(
            "agento.framework.scoped_config.build_scoped_overrides", return_value={}
        ), patch(
            "agento.framework.scoped_config.load_scoped_db_overrides", return_value={}
        ), patch(
            "agento.framework.config_resolver.ScopedConfigService.resolve_all",
            return_value=resolved,
        ):
            conn = MagicMock()
            cursor = MagicMock()
            cursor.fetchone.return_value = {"workspace_id": 1}
            conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
            conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            return get_resolved_fields(conn, module, Scope.AGENT_VIEW, 42)

    def test_only_the_selected_harness_passthrough_is_listed(self):
        paths = [f.path for f in self._fields("agent_view", {"agent_view/harness": "codex"})]
        assert paths == ["agent_view/model", "codex/config"]

    def test_the_other_harness_is_listed_instead_when_it_is_selected(self):
        paths = [f.path for f in self._fields("agent_view", {"agent_view/harness": "claude"})]
        assert paths == ["agent_view/model", "claude/settings"]

    def test_an_unmarked_field_of_a_harness_module_is_never_borrowed(self):
        paths = [f.path for f in self._fields("agent_view", {"agent_view/harness": "codex"})]
        assert "codex/private" not in paths

    def test_all_of_them_show_before_a_harness_is_chosen(self):
        paths = [f.path for f in self._fields("agent_view", {})]
        assert paths == ["agent_view/model", "claude/settings", "codex/config"]

    def test_the_harness_module_node_is_unchanged(self):
        """Borrowing is additive: the field keeps its own module node, and that node does
        not start showing the other harness's fields."""
        paths = [f.path for f in self._fields("codex", {"agent_view/harness": "claude"})]
        assert paths == ["codex/config", "codex/private"]

    def test_the_borrowed_row_keeps_its_own_module_path(self):
        """`e Edit` and `d Delete` in the TUI act on `field.path`, so a borrowed row that
        carried `agent_view/…` would write the value where nothing reads it."""
        rows = {f.path: f for f in self._fields("agent_view", {"agent_view/harness": "codex"})}
        assert rows["codex/config"].field_name == "config"
