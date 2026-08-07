"""Tests for the Skills/Tools admin data layer: enablement resolution + toolset grouping."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from agento.framework.admin.app import AdminApp
from agento.framework.admin.data import (
    EnablementItem,
    _scan_tools_by_toolset,
    get_all_skill_names,
    get_skill_states,
    get_tool_states,
)
from agento.framework.admin.screens._enablement import prompt_label
from agento.framework.admin.screens.skills import SkillsScreen
from agento.framework.admin.screens.tools import ToolsScreen


def _real_core_manifests():
    """Real scanned+enabled core manifests — needed wherever a test exercises the
    module-agnostic config.json fallback, since conftest does not bootstrap()."""
    from agento.framework.bootstrap import CORE_MODULES_DIR
    from agento.framework.module_loader import scan_modules
    from agento.framework.module_status import filter_enabled

    return filter_enabled(scan_modules(CORE_MODULES_DIR))


def _mock_conn(rows=None, raises=False):
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    if raises:
        cursor.execute.side_effect = Exception("no such table: skill_registry")
    elif rows is not None:
        cursor.fetchall.return_value = rows
    return conn, cursor


def _manifest(name, tools):
    m = MagicMock()
    m.name = name
    m.tools = tools
    return m


class TestGetAllSkillNames:
    def test_returns_sorted_names(self):
        conn, _ = _mock_conn(rows=[{"name": "alpha"}, {"name": "beta"}])
        assert get_all_skill_names(conn) == ["alpha", "beta"]

    def test_empty_when_no_conn(self):
        assert get_all_skill_names(None) == []

    def test_graceful_empty_when_table_absent(self):
        conn, _ = _mock_conn(raises=True)
        assert get_all_skill_names(conn) == []


class TestScanToolsByToolset:
    def test_groups_by_toolset_with_fallback_and_cross_module(self):
        manifests = [
            _manifest("bi", [
                {"name": "mssql_bi_stock", "toolset": "BI Warehouse"},
                {"name": "mssql_bi_sales", "toolset": "BI Warehouse"},
            ]),
            _manifest("erp", [
                {"name": "mssql_nav_erp", "toolset": "BI Warehouse"},  # cross-module, same toolset
                {"name": "mysql_wms"},  # no toolset -> falls back to module name "erp"
            ]),
        ]
        # scan_modules is called once per modules dir; return the fakes on the
        # first call and nothing afterwards, regardless of how many dirs exist.
        calls = {"n": 0}

        def _scan(_dir):
            calls["n"] += 1
            return manifests if calls["n"] == 1 else []

        with patch("agento.framework.module_loader.scan_modules", side_effect=_scan), patch(
            "agento.framework.module_status.filter_enabled", side_effect=lambda x: x
        ):
            groups = _scan_tools_by_toolset()

        d = dict(groups)
        # toolsets sorted; "BI Warehouse" (capital B) sorts before "erp"
        assert [ts for ts, _ in groups] == ["BI Warehouse", "erp"]
        # cross-module tools merged under the shared toolset, names sorted
        assert d["BI Warehouse"] == ["mssql_bi_sales", "mssql_bi_stock", "mssql_nav_erp"]
        # fallback to module name when toolset omitted
        assert d["erp"] == ["mysql_wms"]


class TestGetToolStates:
    def test_resolution_and_explicit_here(self):
        merged = {
            "tools/jira_search/is_enabled": ("1", False),
            "tools/jira_create/is_enabled": ("0", False),
        }
        subset = {"tools/jira_search/is_enabled": ("1", False)}  # set at this scope
        conn, _ = _mock_conn()
        with patch(
            "agento.framework.admin.data._scan_tools_by_toolset",
            return_value=[("Jira", ["jira_create", "jira_search"])],
        ), patch(
            "agento.framework.scoped_config.build_scoped_overrides", return_value=merged
        ), patch(
            "agento.framework.scoped_config.load_scoped_db_overrides", return_value=subset
        ):
            groups = get_tool_states(conn, scope="workspace", scope_id=3)

        assert len(groups) == 1
        toolset, items = groups[0]
        assert toolset == "Jira"
        by = {i.name: i for i in items}
        assert by["jira_search"].enabled is True
        assert by["jira_search"].explicit_here is True
        assert by["jira_create"].enabled is False
        assert by["jira_create"].explicit_here is False

    def test_agent_view_scope_includes_workspace_tier(self):
        conn, cursor = _mock_conn()
        cursor.fetchone.return_value = {"workspace_id": 7}
        captured = {}

        def _fake_build(c, agent_view_id=None, workspace_id=None):
            captured["agent_view_id"] = agent_view_id
            captured["workspace_id"] = workspace_id
            return {}

        with patch(
            "agento.framework.admin.data._scan_tools_by_toolset",
            return_value=[("core", ["browser"])],
        ), patch(
            "agento.framework.scoped_config.build_scoped_overrides", side_effect=_fake_build
        ), patch(
            "agento.framework.scoped_config.load_scoped_db_overrides", return_value={}
        ):
            get_tool_states(conn, scope="agent_view", scope_id=5)

        assert captured == {"agent_view_id": 5, "workspace_id": 7}

    def test_missing_row_is_disabled(self):
        # A tool with no config.json default: opt-in means no row -> disabled.
        # (`browser` is NOT such a tool — core/config.json ships it enabled, see below.)
        conn, _ = _mock_conn()
        with patch(
            "agento.framework.admin.data._scan_tools_by_toolset",
            return_value=[("demo", ["demo_no_default"])],
        ), patch(
            "agento.framework.scoped_config.build_scoped_overrides", return_value={}
        ), patch(
            "agento.framework.scoped_config.load_scoped_db_overrides", return_value={}
        ):
            groups = get_tool_states(conn, scope="default", scope_id=0)
        item = groups[0][1][0]
        assert item.name == "demo_no_default"
        assert item.enabled is False  # opt-in: no row -> disabled
        assert item.explicit_here is False

    def test_first_class_config_json_default_resolves_enabled(self):
        """A module may ship a tool enabled by default; Python must see that.

        `core/config.json` enables `browser`. Before the literal-path fallback in
        ScopedConfigService, Python could not resolve module-agnostic
        `tools/<name>/is_enabled` keys and reported such a tool as disabled while the
        toolbox gate ran it — the screen contradicting the runtime.
        """
        conn, _ = _mock_conn()
        with patch(
            "agento.framework.admin.data._scan_tools_by_toolset",
            return_value=[("browser", ["browser"])],
        ), patch(
            "agento.framework.scoped_config.build_scoped_overrides", return_value={}
        ), patch(
            "agento.framework.scoped_config.load_scoped_db_overrides", return_value={}
        ), patch(
            "agento.framework.bootstrap.get_manifests", return_value=_real_core_manifests()
        ):
            groups = get_tool_states(conn, scope="default", scope_id=0)
        item = groups[0][1][0]
        assert item.name == "browser"
        assert item.enabled is True
        assert item.explicit_here is False


class TestGetSkillStates:
    def test_resolution_dash_safe_and_inherited(self):
        merged = {
            "skill/git-workflow/is_enabled": ("1", False),  # dash-named, set here
            "skill/beta/is_enabled": ("1", False),          # enabled via inheritance
            "skill/off/is_enabled": ("0", False),           # explicitly disabled
        }
        subset = {"skill/git-workflow/is_enabled": ("1", False)}
        conn, _ = _mock_conn()
        with patch(
            "agento.framework.admin.data.get_all_skill_names",
            return_value=["beta", "git-workflow", "off"],
        ), patch(
            "agento.framework.scoped_config.build_scoped_overrides", return_value=merged
        ), patch(
            "agento.framework.scoped_config.load_scoped_db_overrides", return_value=subset
        ):
            skills = get_skill_states(conn, scope="workspace", scope_id=3)

        by = {s.name: s for s in skills}
        assert by["git-workflow"].enabled is True  # resolved dash-exact via .overrides
        assert by["git-workflow"].explicit_here is True
        assert by["beta"].enabled is True
        assert by["beta"].explicit_here is False  # inherited
        assert by["off"].enabled is False


class TestPromptLabel:
    def test_inherited_enable_is_annotated(self):
        inherited = EnablementItem(name="jira", path="tools/jira/is_enabled", enabled=True, explicit_here=False)
        local = EnablementItem(name="jira", path="tools/jira/is_enabled", enabled=True, explicit_here=True)
        disabled = EnablementItem(name="jira", path="tools/jira/is_enabled", enabled=False, explicit_here=False)
        assert "(inherited)" in prompt_label(inherited)
        assert prompt_label(local) == "jira"
        assert prompt_label(disabled) == "jira"


class TestScreenRegistration:
    def test_skills_and_tools_registered(self):
        assert AdminApp.SCREENS.get("skills") is SkillsScreen
        assert AdminApp.SCREENS.get("tools") is ToolsScreen
        assert "access" not in AdminApp.SCREENS

    def test_item_dataclass(self):
        item = EnablementItem(name="t", path="tools/t/is_enabled", enabled=True, explicit_here=False)
        assert item.path == "tools/t/is_enabled"
