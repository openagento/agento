"""`requires` — a declared toolset-master relationship, validated and displayed."""
from __future__ import annotations

import json
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.admin.data import EnablementItem
from agento.framework.admin.screens._enablement import prompt_label
from agento.framework.module_validator import validate_module
from agento.framework.tool_enablement import blocked_by, is_effective, is_effective_for_view, scan_tool_requires

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
        # Patched at `scan_all_modules`, the ONE discovery path: a per-root scan here would
        # pass while an installed extension's tool requirements stayed unenforced.
        with patch("agento.framework.module_discovery.scan_all_modules", return_value=[m]), \
             patch("agento.framework.module_status.filter_enabled", side_effect=lambda x: x):
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


class TestIsEffectiveForView:
    """The toolbox gate's answer for one view: its own value and its requires chain, with the
    chain read from the bootstrapped manifests (no module scan on a hot path)."""

    class _Svc:
        values: ClassVar[dict] = {}

        def __init__(self, conn, scope, scope_id, *, strict):
            assert (scope, scope_id, strict) == ("agent_view", 7, True)   # fails closed (SEC-9)

        def get(self, path):
            return self.values.get(path)

    def _run(self, values, manifests=None):
        if manifests is None:
            manifests = [MagicMock(tools=[_t("demo"), _t("demo_a", requires="demo")])]
        self._Svc.values = values
        with patch("agento.framework.bootstrap.get_manifests", return_value=manifests), \
             patch("agento.framework.config_resolver.ScopedConfigService", self._Svc):
            return is_effective_for_view(object(), "demo_a", agent_view_id=7)

    @pytest.mark.parametrize(("values", "expected"), [
        ({"tools/demo_a/is_enabled": "1", "tools/demo/is_enabled": "1"}, True),
        ({"tools/demo_a/is_enabled": "1", "tools/demo/is_enabled": "0"}, False),   # master off
        ({}, False),                                                              # unset is off
    ])
    def test_own_value_and_chain(self, values, expected):
        assert self._run(values) is expected

    def test_no_bootstrapped_manifests_is_an_error_not_an_unchecked_chain(self):
        with pytest.raises(RuntimeError):
            self._run({"tools/demo_a/is_enabled": "1"}, manifests=[])


class TestStrictScopedConfig:
    """`strict=True`: an unreadable level raises; it never lets a less specific level answer."""

    @staticmethod
    def _conn(*, fail_on=None, view_row=True):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value

        def execute(sql, params=()):
            if fail_on and fail_on(sql, params):
                raise RuntimeError("query failed")
            cur.fetchone.return_value = {"workspace_id": 2} if view_row else None
            cur.fetchall.return_value = []

        cur.execute.side_effect = execute
        return conn

    @pytest.mark.parametrize("fail_on", [
        lambda sql, p: "FROM agent_view" in sql,                              # the workspace lookup
        lambda sql, p: "core_config_data" in sql and p[0] == "agent_view",     # the view level
        lambda sql, p: "core_config_data" in sql and p[0] == "workspace",      # the workspace level
        lambda sql, p: "core_config_data" in sql and p[0] == "default",        # the default level
    ])
    def test_a_failed_level_raises(self, fail_on):
        from agento.framework.config_resolver import ScopedConfigService

        with pytest.raises(RuntimeError):
            ScopedConfigService(self._conn(fail_on=fail_on), "agent_view", 7, strict=True)
        ScopedConfigService(self._conn(fail_on=fail_on), "agent_view", 7)    # lenient: as before

    def test_a_missing_view_raises(self):
        from agento.framework.config_resolver import ScopedConfigService

        with pytest.raises(LookupError):
            ScopedConfigService(self._conn(view_row=False), "agent_view", 7, strict=True)
