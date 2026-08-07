"""`requires` — a declared toolset-master relationship, validated and displayed."""
from __future__ import annotations

import json
from typing import ClassVar
from unittest.mock import MagicMock, patch

from agento.framework.admin.data import EnablementItem
from agento.framework.admin.screens._enablement import prompt_label
from agento.framework.module_validator import validate_module
from agento.framework.tool_enablement import blocked_by, is_effective, scan_tool_requires

BASE = {"name": "demo", "version": "1.0.0", "description": "d"}


def _mk(tmp_path, tools):
    d = tmp_path / "demo"
    d.mkdir()
    (d / "module.json").write_text(json.dumps({**BASE, "tools": tools}))
    return d


def _t(name, **extra):
    return {"type": "mcp", "name": name, "description": "d", "toolset": "demo", **extra}


class TestValidation:
    def test_valid_requires_is_accepted(self, tmp_path):
        d = _mk(tmp_path, [_t("demo"), _t("demo_a", requires="demo")])
        assert validate_module(d) == []

    def test_requires_an_undeclared_tool_is_rejected(self, tmp_path):
        d = _mk(tmp_path, [_t("demo_a", requires="nope")])
        assert any("'requires' must name a tool declared" in e for e in validate_module(d))

    def test_self_reference_is_rejected(self, tmp_path):
        d = _mk(tmp_path, [_t("demo_a", requires="demo_a")])
        assert any("self-referential" in e for e in validate_module(d))

    def test_non_string_requires_is_rejected(self, tmp_path):
        d = _mk(tmp_path, [_t("demo"), _t("demo_a", requires=7)])
        assert any("'requires'" in e for e in validate_module(d))

    def test_two_node_cycle_is_rejected(self, tmp_path):
        d = _mk(tmp_path, [_t("a", requires="b"), _t("b", requires="a")])
        assert any("'requires' cycle" in e for e in validate_module(d))

    def test_three_node_cycle_is_rejected(self, tmp_path):
        d = _mk(tmp_path, [_t("a", requires="b"), _t("b", requires="c"), _t("c", requires="a")])
        assert any("'requires' cycle" in e for e in validate_module(d))

    def test_a_chain_without_a_cycle_is_accepted(self, tmp_path):
        d = _mk(tmp_path, [_t("a"), _t("b", requires="a"), _t("c", requires="b")])
        assert validate_module(d) == []

    def test_diamond_without_a_cycle_is_accepted(self, tmp_path):
        d = _mk(tmp_path, [_t("a"), _t("b", requires="a"), _t("c", requires="a")])
        assert validate_module(d) == []


class TestBlockedBy:
    REQUIRES: ClassVar[dict[str, str]] = {"jira_search": "jira"}

    def test_none_when_master_enabled(self):
        assert blocked_by("jira_search", self.REQUIRES, lambda n: True) is None

    def test_names_the_master_when_it_is_off(self):
        assert blocked_by("jira_search", self.REQUIRES, lambda n: n != "jira") == "jira"

    def test_tool_without_requires_is_never_blocked(self):
        assert blocked_by("email_send", {}, lambda n: False) is None

    def test_own_value_does_not_block_itself(self):
        """blocked_by describes ancestors only; the tool's own '0' is plain 'disabled'."""
        assert blocked_by("jira_search", self.REQUIRES, lambda n: n == "jira") is None

    def test_multi_level_chain_reports_nearest_disabled_ancestor(self):
        req = {"c": "b", "b": "a"}
        assert blocked_by("c", req, lambda n: n != "b") == "b"
        assert blocked_by("c", req, lambda n: n != "a") == "a"

    def test_cycle_fails_closed(self):
        """The JS gate returns false on a cycle, so Python must not report unblocked.

        Validation rejects cycles, so this is the defensive path for a manifest that
        bypassed it (e.g. hand-edited on a deployment).
        """
        req = {"x": "y", "y": "x"}
        assert blocked_by("x", req, lambda n: True) is not None
        assert is_effective("x", req, lambda n: True) is False

    def test_self_cycle_fails_closed(self):
        assert blocked_by("x", {"x": "x"}, lambda n: True) is not None


class TestIsEffective:
    REQUIRES: ClassVar[dict[str, str]] = {"jira_search": "jira"}

    def test_true_when_own_key_and_master_are_on(self):
        assert is_effective("jira_search", self.REQUIRES, lambda n: True) is True

    def test_false_when_own_key_is_off(self):
        assert is_effective("jira_search", self.REQUIRES, lambda n: n == "jira") is False

    def test_false_when_master_is_off_even_though_own_key_is_on(self):
        assert is_effective("jira_search", self.REQUIRES, lambda n: n != "jira") is False


class TestLabel:
    def test_blocked_annotation(self):
        item = EnablementItem("jira_search", "tools/jira_search/is_enabled", True, True, blocked_by="jira")
        assert prompt_label(item) == "jira_search  (blocked by jira)"

    def test_inherited_annotation_unchanged(self):
        item = EnablementItem("jira_search", "tools/jira_search/is_enabled", True, False)
        assert prompt_label(item) == "jira_search  (inherited)"

    def test_blocked_wins_over_inherited(self):
        item = EnablementItem("jira_search", "tools/jira_search/is_enabled", True, False, blocked_by="jira")
        assert prompt_label(item) == "jira_search  (blocked by jira)"

    def test_plain_name_when_neither(self):
        item = EnablementItem("email_send", "tools/email_send/is_enabled", True, True)
        assert prompt_label(item) == "email_send"


class TestScanRequires:
    def test_reads_requires_from_enabled_manifests(self):
        m = MagicMock()
        m.name = "demo"
        m.tools = [_t("demo"), _t("demo_a", requires="demo")]
        with patch("agento.framework.module_loader.scan_modules", return_value=[m]), \
             patch("agento.framework.module_status.filter_enabled", side_effect=lambda x: x), \
             patch("agento.framework.tool_enablement.Path") as p:
            p.return_value.is_dir.return_value = True
            assert scan_tool_requires() == {"demo_a": "demo"}


class TestToolListStatus:
    """`tool:list` must print the EFFECTIVE status, not the child's own value."""

    def test_master_off_child_on_prints_disabled_with_reason(self, capsys):
        from agento.modules.agent_view.src.commands.tool_list import ToolListCommand

        manifest = MagicMock()
        manifest.name = "jira"
        manifest.tools = [_t("jira"), _t("jira_search", requires="jira")]
        values = {"tools/jira/is_enabled": "0", "tools/jira_search/is_enabled": "1"}
        svc = MagicMock()
        svc.get.side_effect = values.get

        with patch("agento.framework.bootstrap.get_manifests", return_value=[manifest]), \
             patch("agento.framework.config_resolver.ScopedConfigService", return_value=svc), \
             patch("agento.framework.tool_enablement.scan_tool_requires",
                   return_value={"jira_search": "jira"}), \
             patch("agento.framework.cli.runtime._load_framework_config",
                   return_value=({}, None, None)), \
             patch("agento.framework.db.get_connection", return_value=MagicMock()):
            ToolListCommand().execute(MagicMock(agent_view_code=None))

        out = capsys.readouterr().out
        assert "jira_search" in out
        assert "disabled (blocked by jira)" in out
        assert "enabled (blocked by jira)" not in out
