"""The labels the in-tree modules ship (PRD §3, §7)."""
from __future__ import annotations

import json
from pathlib import Path

from agento.framework.bootstrap import CORE_MODULES_DIR
from agento.framework.tool_catalog import RISK_KEYS, STUB_IN_TEST

# The tools whose `tools/<name>/sensitive` default was "1" before labels existed.
OLD_SENSITIVE = frozenset({
    "bitbucket_add_comment", "bitbucket_resolve_comment", "bitbucket_set_review", "bitbucket_create_pr",
    "email_send", "browser_evaluate", "browser_run_code", "browser_click", "browser_type",
    "browser_fill_form", "browser_select_option", "browser_press_key", "browser_drag",
    "browser_file_upload", "browser_handle_dialog",
    "github_add_comment", "github_resolve_thread", "github_set_review", "github_create_pr",
    "jira_add_comment", "jira_transition_issue", "jira_assign_issue", "jira_attach_file",
    "jira_create_issue", "jira_update_issue",
    "outlook_reply", "outlook_mark_processed", "outlook_send_mail",
    "versioned_artifact_init", "versioned_artifact_create_draft", "versioned_artifact_save_version",
    "versioned_artifact_publish", "versioned_artifact_discard_draft",
})


def _in_tree():
    for manifest in sorted(Path(CORE_MODULES_DIR).glob("*/module.json")):
        config_file = manifest.parent / "config.json"
        config = json.loads(config_file.read_text()) if config_file.is_file() else {}
        yield json.loads(manifest.read_text()).get("tools", []), config


def _labels() -> dict[str, set[str]]:
    return {t["name"]: set(t.get("labels", [])) for tools, _ in _in_tree() for t in tools}


def test_builtin_stub_in_test_equals_old_sensitive_set():
    assert len(OLD_SENSITIVE) == 33
    assert {name for name, labels in _labels().items() if STUB_IN_TEST in labels} == OLD_SENSITIVE


def test_sensitive_keys_match_stub_in_test_labels():
    for tools, config in _in_tree():
        for t in tools:
            sensitive = config.get(f"tools/{t['name']}/sensitive") == "1"
            assert sensitive is (STUB_IN_TEST in t.get("labels", [])), t["name"]


def test_single_tool_trifecta_browser_run_code():
    labels = _labels()
    for name in ("browser_evaluate", "browser_run_code"):
        assert set(RISK_KEYS) <= labels[name], name


def test_browser_trifecta_without_scripting_tools():
    labels = _labels()
    assert set(RISK_KEYS) <= labels["browser_navigate"] | labels["browser_snapshot"]
