"""Migration: core/playwright_tool_whitelist -> per-tool tools/<name>/is_enabled."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

PATCH_PATH = (
    Path(__file__).resolve().parents[4]
    / "src/agento/modules/core/src/patches/migrate_playwright_whitelist_to_tools.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("migrate_playwright_whitelist_to_tools", PATCH_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self._last = []

    def execute(self, sql, params=()):
        upper = sql.strip().upper()
        if upper.startswith("SELECT"):
            self._last = [
                {"scope": r["scope"], "scope_id": r["scope_id"], "value": r["value"]}
                for r in self.rows if r["path"] == params[0]
            ]
        elif "INSERT" in upper:
            scope, scope_id, path, value, encrypted = params
            self.rows = [
                r for r in self.rows
                if not (r["scope"] == scope and r["scope_id"] == scope_id and r["path"] == path)
            ]
            self.rows.append({"scope": scope, "scope_id": scope_id, "path": path,
                              "value": value, "encrypted": encrypted})
        elif upper.startswith("DELETE"):
            self.rows = [r for r in self.rows if r["path"] != params[0]]

    def fetchall(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def _row(scope, scope_id, path, value):
    return {"scope": scope, "scope_id": scope_id, "path": path, "value": value, "encrypted": 0}


def _paths(conn):
    return {(r["scope"], r["scope_id"], r["path"]): r["value"] for r in conn.cur.rows}


def test_whitelisted_tools_become_enabled_rows_at_the_same_scope():
    mod = _load()
    conn = FakeConn([_row("agent_view", 4, "core/playwright_tool_whitelist",
                          "browser_navigate,browser_snapshot")])
    mod.MigratePlaywrightWhitelistToTools().apply(conn)
    result = _paths(conn)
    assert result[("agent_view", 4, "tools/browser_navigate/is_enabled")] == "1"
    assert result[("agent_view", 4, "tools/browser_snapshot/is_enabled")] == "1"
    assert ("agent_view", 4, "core/playwright_tool_whitelist") not in result
    assert conn.commits == 1


def test_every_known_tool_gets_an_explicit_value_at_that_scope():
    """The whitelist was ONE scope-resolved value, so a narrower child list REPLACED the
    parent's. Per-tool keys resolve independently, so unlisted names must be written '0' —
    otherwise a child would inherit the parent's extra tools and access would broaden."""
    mod = _load()
    conn = FakeConn([_row("agent_view", 4, "core/playwright_tool_whitelist", "browser_navigate")])
    mod.MigratePlaywrightWhitelistToTools().apply(conn)
    result = _paths(conn)
    written = {p for (_s, _i, p) in result}
    assert len(written) == len(mod.BROWSER_TOOL_NAMES)
    assert result[("agent_view", 4, "tools/browser_navigate/is_enabled")] == "1"
    assert result[("agent_view", 4, "tools/browser_snapshot/is_enabled")] == "0"
    assert result[("agent_view", 4, "tools/browser_run_code/is_enabled")] == "0"


def test_a_narrower_child_list_does_not_inherit_the_parents_tools():
    """The regression this guards: default allows browser_navigate, the view allows only
    browser_snapshot. Before the fix the view ended up with BOTH."""
    mod = _load()
    conn = FakeConn([
        _row("default", 0, "core/playwright_tool_whitelist", "browser_navigate"),
        _row("agent_view", 4, "core/playwright_tool_whitelist", "browser_snapshot"),
    ])
    mod.MigratePlaywrightWhitelistToTools().apply(conn)
    result = _paths(conn)
    # The view explicitly denies the parent's tool, so the inherited '1' cannot win.
    assert result[("agent_view", 4, "tools/browser_navigate/is_enabled")] == "0"
    assert result[("agent_view", 4, "tools/browser_snapshot/is_enabled")] == "1"
    assert result[("default", 0, "tools/browser_navigate/is_enabled")] == "1"
    assert result[("default", 0, "tools/browser_snapshot/is_enabled")] == "0"


def test_values_are_trimmed_and_lowercased_like_parselist():
    """browser.js parsed with `.trim().toLowerCase()` — the migration must match."""
    mod = _load()
    conn = FakeConn([_row("default", 0, "core/playwright_tool_whitelist",
                          " Browser_Navigate , browser_snapshot ")])
    mod.MigratePlaywrightWhitelistToTools().apply(conn)
    result = _paths(conn)
    assert result[("default", 0, "tools/browser_navigate/is_enabled")] == "1"
    assert result[("default", 0, "tools/browser_snapshot/is_enabled")] == "1"


def test_an_empty_whitelist_denies_every_tool_and_is_removed():
    """An empty list meant "deny all" — that must survive as explicit '0's, not as
    absent keys that could inherit a grant from a wider scope."""
    mod = _load()
    conn = FakeConn([_row("agent_view", 4, "core/playwright_tool_whitelist", "")])
    mod.MigratePlaywrightWhitelistToTools().apply(conn)
    result = _paths(conn)
    assert ("agent_view", 4, "core/playwright_tool_whitelist") not in result
    assert set(result.values()) == {"0"}
    assert len(result) == len(mod.BROWSER_TOOL_NAMES)


def test_an_unknown_tool_name_is_skipped():
    """Only names this Playwright version actually exposes are migrated."""
    mod = _load()
    conn = FakeConn([_row("default", 0, "core/playwright_tool_whitelist",
                          "browser_navigate,browser_not_a_tool")])
    mod.MigratePlaywrightWhitelistToTools().apply(conn)
    result = _paths(conn)
    assert result[("default", 0, "tools/browser_navigate/is_enabled")] == "1"
    assert ("default", 0, "tools/browser_not_a_tool/is_enabled") not in result


def test_no_whitelist_rows_is_a_noop():
    mod = _load()
    conn = FakeConn([_row("default", 0, "core/timezone", "UTC")])
    mod.MigratePlaywrightWhitelistToTools().apply(conn)
    assert _paths(conn) == {("default", 0, "core/timezone"): "UTC"}


def test_multiple_scopes_migrate_independently():
    mod = _load()
    conn = FakeConn([
        _row("default", 0, "core/playwright_tool_whitelist", "browser_navigate"),
        _row("agent_view", 2, "core/playwright_tool_whitelist", "browser_snapshot"),
    ])
    mod.MigratePlaywrightWhitelistToTools().apply(conn)
    result = _paths(conn)
    assert result[("default", 0, "tools/browser_navigate/is_enabled")] == "1"
    assert result[("agent_view", 2, "tools/browser_snapshot/is_enabled")] == "1"
    assert result[("agent_view", 2, "tools/browser_navigate/is_enabled")] == "0"


class TestEnvWhitelistIsNotMigrated:
    """An ENV whitelist is deliberately not migrated: it cannot be read reliably (this patch
    runs in the cron container, the variable belongs to the toolbox container) and turning it
    into permanent DB grants would widen durable state. Failing toward disabled is what
    docker/README.md and DECISIONS.md promise."""

    def test_env_value_creates_no_grants(self, monkeypatch):
        mod = _load()
        monkeypatch.setenv("CONFIG__CORE__PLAYWRIGHT_TOOL_WHITELIST", "browser_snapshot")
        conn = FakeConn([])
        mod.MigratePlaywrightWhitelistToTools().apply(conn)
        assert _paths(conn) == {}

    def test_env_does_not_change_how_db_rows_are_translated(self, monkeypatch):
        mod = _load()
        monkeypatch.setenv("CONFIG__CORE__PLAYWRIGHT_TOOL_WHITELIST", "browser_run_code")
        conn = FakeConn([_row("default", 0, "core/playwright_tool_whitelist", "browser_snapshot")])
        mod.MigratePlaywrightWhitelistToTools().apply(conn)
        result = _paths(conn)
        assert result[("default", 0, "tools/browser_snapshot/is_enabled")] == "1"
        assert result[("default", 0, "tools/browser_run_code/is_enabled")] == "0"


class TestStalePerToolRowsAreCleared:
    """tool:enable validates only a name's shape, so tools/browser_*/is_enabled rows could
    already exist at scopes the whitelist ignored — dead then, live once it is gone."""

    def test_a_stale_grant_at_another_scope_does_not_survive(self):
        mod = _load()
        conn = FakeConn([
            _row("default", 0, "core/playwright_tool_whitelist", "browser_snapshot"),
            _row("agent_view", 4, "tools/browser_run_code/is_enabled", "1"),
        ])
        mod.MigratePlaywrightWhitelistToTools().apply(conn)
        result = _paths(conn)
        assert ("agent_view", 4, "tools/browser_run_code/is_enabled") not in result
        assert result[("default", 0, "tools/browser_run_code/is_enabled")] == "0"
        assert result[("default", 0, "tools/browser_snapshot/is_enabled")] == "1"

    def test_stale_rows_are_cleared_even_with_no_legacy_whitelist_anywhere(self):
        """No whitelist row means the whitelist denied everything, so a stray per-tool
        grant must not become live either."""
        mod = _load()
        conn = FakeConn([_row("agent_view", 4, "tools/browser_run_code/is_enabled", "1")])
        mod.MigratePlaywrightWhitelistToTools().apply(conn)
        assert _paths(conn) == {}

    def test_unrelated_tool_rows_are_left_alone(self):
        mod = _load()
        conn = FakeConn([
            _row("default", 0, "tools/jira_search/is_enabled", "1"),
            _row("default", 0, "core/timezone", "UTC"),
        ])
        mod.MigratePlaywrightWhitelistToTools().apply(conn)
        result = _paths(conn)
        assert result[("default", 0, "tools/jira_search/is_enabled")] == "1"
        assert result[("default", 0, "core/timezone")] == "UTC"


def test_browser_tool_names_matches_the_manifest():
    mod = _load()
    manifest = json.loads((PATCH_PATH.parents[2] / "module.json").read_text())
    declared = {t["name"] for t in manifest["tools"] if t["name"].startswith("browser_")}
    assert set(mod.BROWSER_TOOL_NAMES) == declared


def test_require_returns_empty_list():
    assert _load().MigratePlaywrightWhitelistToTools().require() == []


def test_patch_is_registered_in_data_patch_json():
    dp = json.loads((PATCH_PATH.parents[2] / "data_patch.json").read_text())
    assert "MigratePlaywrightWhitelistToTools" in [p["name"] for p in dp["patches"]]
