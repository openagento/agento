"""Every core-module toolbox tool key must be visible AND correctly resolved.

The Tools screen is the union of module.json tools[] names, so a tool a module
registers but does not declare is invisible and uncontrollable. And a declared
tool whose config.json default Python cannot resolve is reported as disabled
while being live — see the literal-path fallback in ScopedConfigService.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parents[4]
MODULES = REPO / "src/agento/modules"
TOOLBOX = REPO / "src/agento/toolbox"

# The 7 tools browser.js wraps itself (BROWSER_TOOLS); the rest are upstream passthrough.
WRAPPED = {
    "browser_navigate", "browser_wait_for", "browser_take_screenshot",
    "browser_snapshot", "browser_evaluate", "browser_start_video", "browser_stop_video",
}


def _manifest_tools(module: str) -> list[dict]:
    return json.loads((MODULES / module / "module.json").read_text()).get("tools", [])


def _declared(module: str) -> set[str]:
    return {t["name"] for t in _manifest_tools(module)}


def _real_manifests():
    """Real scanned+enabled core manifests. conftest does not bootstrap, so any code
    path reading get_manifests() must be given them explicitly."""
    from agento.framework.bootstrap import CORE_MODULES_DIR
    from agento.framework.module_loader import scan_modules
    from agento.framework.module_status import filter_enabled

    return filter_enabled(scan_modules(CORE_MODULES_DIR))


def test_core_declares_its_two_plain_tools():
    assert _declared("core") >= {"email_send", "schedule_followup"}


def test_core_declares_the_browser_master_and_every_browser_tool():
    declared = _declared("core")
    assert "browser" in declared
    browser_tools = {n for n in declared if n.startswith("browser_")}
    assert len(browser_tools) == 26, sorted(browser_tools)   # the real listTools() set
    assert browser_tools >= WRAPPED


def test_every_browser_tool_requires_the_master_and_is_grouped_under_it():
    for tool in _manifest_tools("core"):
        if not tool["name"].startswith("browser_"):
            continue
        assert tool["type"] == "mcp"
        assert tool["toolset"] == "browser"
        assert tool["requires"] == "browser", tool["name"]
        assert tool["description"]


def test_the_browser_master_itself_requires_nothing():
    master = next(t for t in _manifest_tools("core") if t["name"] == "browser")
    assert master["toolset"] == "browser"
    assert "requires" not in master


def test_no_dynamic_prefix_field_survives_anywhere():
    """Every tool is named explicitly; there is no pattern-based membership."""
    for path in MODULES.glob("*/module.json"):
        for tool in json.loads(path.read_text()).get("tools", []):
            assert "dynamic_prefix" not in tool, path


def test_every_wrapped_browser_tool_is_declared():
    """browser.js's own BROWSER_TOOLS wrappers must all be declared.

    The full "declared == what Playwright actually exposes" check lives in
    toolbox/tests/tool-declaration.test.js, which holds the real `listTools()` inventory taken
    under production flags (`--caps devtools`) — the package README lists a different set.
    """
    js = (MODULES / "core/toolbox/browser.js").read_text()
    block = js[js.index("const BROWSER_TOOLS = {"):]
    wrapped = set(re.findall(r"^  (browser_[a-z_]+):", block, re.M))
    assert wrapped == WRAPPED, wrapped
    assert wrapped <= _declared("core")


def test_playwright_tool_whitelist_is_gone():
    system = json.loads((MODULES / "core/system.json").read_text())
    assert "playwright_tool_whitelist" not in system
    js = (MODULES / "core/toolbox/browser.js").read_text()
    assert "playwright_tool_whitelist" not in js
    assert "toolWhitelist" not in js


def test_browser_tools_ship_no_config_default():
    """The whitelist shipped empty (deny all), so per-tool keys must stay opt-in-off —
    otherwise this change would GRANT every browser tool on upgrade."""
    config = json.loads((MODULES / "core/config.json").read_text())
    assert config["tools/browser/is_enabled"] == "1"      # unchanged master default
    assert not [k for k in config if k.startswith("tools/browser_")]


def test_scan_tools_by_toolset_exposes_core_jira_and_browser_tools():
    from agento.framework.admin.data import _scan_tools_by_toolset

    groups = dict(_scan_tools_by_toolset())
    assert "email_send" in groups.get("core", [])
    assert "schedule_followup" in groups.get("core", [])
    assert "jira_add_comment" in groups.get("jira", [])
    browser = groups.get("browser", [])
    assert "browser" in browser
    assert "browser_navigate" in browser
    assert len(browser) == 27   # master + 26


def test_first_class_defaults_resolve_as_enabled_on_an_empty_db():
    """End-to-end: real manifests + real config.json, no DB rows."""
    from agento.framework.admin.data import get_tool_states

    with patch("agento.framework.scoped_config.build_scoped_overrides", return_value={}), \
         patch("agento.framework.scoped_config.load_scoped_db_overrides", return_value={}), \
         patch("agento.framework.bootstrap.get_manifests", return_value=_real_manifests()), \
         patch("agento.framework.admin.data._ensure_conn"):
        items = {
            item.name: item
            for _toolset, group in get_tool_states(MagicMock())
            for item in group
        }

    for name in ("email_send", "schedule_followup", "browser", "jira",
                 "jira_search", "jira_get_issue", "jira_add_comment",
                 "jira_transition_issue", "jira_assign_issue", "jira_attach_file",
                 "jira_create_issue", "jira_update_issue"):
        assert items[name].enabled is True, f"{name} is live but reported disabled"
        assert items[name].blocked_by is None

    assert items["jira_get_attachment"].enabled is False
    # Every browser tool is off by default, exactly as the empty whitelist made them.
    assert items["browser_navigate"].enabled is False
    assert items["browser_run_code"].enabled is False


def test_children_are_reported_blocked_when_their_master_is_off():
    """The master relationship must be visible, not silently contradicted."""
    from agento.framework.admin.data import get_tool_states

    overrides = {
        "tools/jira/is_enabled": ("0", False),
        "tools/browser/is_enabled": ("0", False),
        "tools/browser_navigate/is_enabled": ("1", False),
    }
    with patch("agento.framework.scoped_config.build_scoped_overrides", return_value=overrides), \
         patch("agento.framework.scoped_config.load_scoped_db_overrides", return_value={}), \
         patch("agento.framework.bootstrap.get_manifests", return_value=_real_manifests()), \
         patch("agento.framework.admin.data._ensure_conn"):
        items = {
            item.name: item
            for _toolset, group in get_tool_states(MagicMock())
            for item in group
        }

    assert items["jira_search"].enabled is True          # its own key is still '1'
    assert items["jira_search"].blocked_by == "jira"     # but it is denied at runtime
    assert items["browser_navigate"].enabled is True
    assert items["browser_navigate"].blocked_by == "browser"
    assert items["email_send"].blocked_by is None
