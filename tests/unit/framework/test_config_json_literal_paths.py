"""Module-agnostic config.json keys (e.g. tools/<name>/is_enabled) must resolve.

`_parse_config_path` splits on the first '/', so `tools/jira_search/is_enabled`
parses as module "tools" — a module that does not exist. Python therefore could
not see the `config.json` defaults the JS gate honours, and the admin Tools
screen reported live tools as disabled. Mirrors the toolbox's loadConfigDefaults(),
including its last-module-wins merge order.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agento.framework.config_resolver import ScopedConfigService


@pytest.fixture
def patched_overrides():
    with patch("agento.framework.scoped_config.build_scoped_overrides") as merged, \
         patch("agento.framework.scoped_config.load_scoped_db_overrides") as scope_only:
        merged.return_value = {}
        scope_only.return_value = {}
        yield merged, scope_only


def _manifest(name: str, path: str = "/fake"):
    m = MagicMock()
    m.name = name
    m.path = path
    return m


def test_literal_tool_default_resolves(patched_overrides):
    with patch("agento.framework.bootstrap.get_manifests", return_value=[_manifest("jira")]), \
         patch("agento.framework.config_resolver.read_config_defaults",
               return_value={"tools/jira_search/is_enabled": "1"}):
        svc = ScopedConfigService(MagicMock())
        assert svc.get("tools/jira_search/is_enabled") == "1"


def test_missing_literal_key_stays_none(patched_overrides):
    with patch("agento.framework.bootstrap.get_manifests", return_value=[_manifest("jira")]), \
         patch("agento.framework.config_resolver.read_config_defaults", return_value={}):
        svc = ScopedConfigService(MagicMock())
        assert svc.get("tools/jira_get_attachment/is_enabled") is None


def test_non_string_default_is_stringified(patched_overrides):
    with patch("agento.framework.bootstrap.get_manifests", return_value=[_manifest("core")]), \
         patch("agento.framework.config_resolver.read_config_defaults",
               return_value={"tools/browser/is_enabled": 1}):
        svc = ScopedConfigService(MagicMock())
        assert svc.get("tools/browser/is_enabled") == "1"


def test_db_override_still_wins_over_literal_default(patched_overrides):
    merged, _ = patched_overrides
    merged.return_value = {"tools/jira_search/is_enabled": ("0", False)}
    with patch("agento.framework.bootstrap.get_manifests", return_value=[_manifest("jira")]), \
         patch("agento.framework.config_resolver.read_config_defaults",
               return_value={"tools/jira_search/is_enabled": "1"}):
        svc = ScopedConfigService(MagicMock())
        assert svc.get("tools/jira_search/is_enabled") == "0"


def test_env_still_wins_over_literal_default(monkeypatch, patched_overrides):
    monkeypatch.setenv("CONFIG__TOOLS__JIRA_SEARCH__IS_ENABLED", "0")
    with patch("agento.framework.bootstrap.get_manifests", return_value=[_manifest("jira")]), \
         patch("agento.framework.config_resolver.read_config_defaults",
               return_value={"tools/jira_search/is_enabled": "1"}):
        svc = ScopedConfigService(MagicMock())
        assert svc.get("tools/jira_search/is_enabled") == "0"


def test_module_form_path_is_unaffected(patched_overrides):
    """The existing module-parsing route must still win for module-scoped keys."""
    with patch("agento.framework.bootstrap.get_manifests", return_value=[_manifest("core")]), \
         patch("agento.framework.config_resolver.read_config_defaults",
               return_value={"timezone": "UTC"}):
        svc = ScopedConfigService(MagicMock())
        assert svc.get("core/timezone") == "UTC"


def test_last_declaring_module_wins_like_object_assign(patched_overrides):
    """loadConfigDefaults() uses Object.assign, so the last module wins. Mirror it.

    Task 5 makes duplicate declarations a validation error, so this only matters
    when validation is bypassed — but the two languages must not diverge.
    """
    def fake_defaults(path):
        return {"tools/dup_tool/is_enabled": "1" if str(path) == "/a" else "0"}

    with patch("agento.framework.bootstrap.get_manifests",
               return_value=[_manifest("a", "/a"), _manifest("b", "/b")]), \
         patch("agento.framework.config_resolver.read_config_defaults", side_effect=fake_defaults):
        svc = ScopedConfigService(MagicMock())
        assert svc.get("tools/dup_tool/is_enabled") == "0"
