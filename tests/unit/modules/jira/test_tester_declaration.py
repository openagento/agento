"""Jira credentials are testable from the admin TUI via the built-in http probe."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

MODULE_DIR = Path("src/agento/modules/jira")
API_FIELDS = ("jira_host", "jira_user", "jira_token")


@pytest.fixture(scope="module")
def system():
    return json.loads((MODULE_DIR / "system.json").read_text())


def test_the_api_credential_fields_carry_the_declaration(system):
    for field in API_FIELDS:
        assert system[field].get("tester", {}).get("kind") == "http", field


def test_the_api_declaration_uses_basic_auth_not_bearer(system):
    """Jira Cloud API tokens are Basic-auth. A Bearer header 401s on a good
    token — which is exactly the false FAIL this feature must not produce."""
    spec = system["jira_token"]["tester"]
    assert spec["basic"] == ["{jira/jira_user}", "{jira/jira_token}"]
    assert "bearer" not in spec
    assert "headers" not in spec or "Authorization" not in spec.get("headers", {})


def test_the_api_declaration_targets_myself(system):
    spec = system["jira_token"]["tester"]
    assert spec["url"] == "{jira/jira_host}/rest/api/2/myself"


def test_the_declaration_expects_200_explicitly(system):
    """`expect` defaults to 200 in the probe, but a credential test is exactly
    where an implicit default should be written down."""
    assert system["jira_token"]["tester"]["expect"] == 200


def test_the_admin_token_is_tested_against_the_admin_user(system):
    """Not jira_user: the admin token belongs to jira_admin_user, and pairing a
    token with the wrong account is a 401 that means nothing."""
    spec = system["jira_admin_token"]["tester"]
    assert spec["kind"] == "http"
    assert spec["basic"] == ["{jira/jira_admin_user}", "{jira/jira_admin_token}"]


def test_every_referenced_field_exists(system):
    declared = set(system)
    for field, schema in system.items():
        spec = schema.get("tester")
        if not isinstance(spec, dict):
            continue
        for value in spec.values():
            for item in (value if isinstance(value, list) else [value]):
                if isinstance(item, str) and "{jira/" in item:
                    name = item.split("{jira/", 1)[1].split("}", 1)[0]
                    assert name in declared, f"{field} -> {item}"


def test_the_token_fields_are_still_obscure(system):
    assert system["jira_token"]["type"] == "obscure"
    assert system["jira_admin_token"]["type"] == "obscure"


def test_fields_that_are_not_credentials_have_no_tester(system):
    """`t` on a project list or a rate limit would offer a test that proves
    nothing. Only the credential path gets the button."""
    for field in ("enabled", "jira_projects", "todo_statuses",
                  "create_issue_limit_per_hour", "jira_assignee"):
        assert "tester" not in system[field], field


def test_the_tester_resolves_as_a_toolbox_arm():
    from agento.framework.config_test import KIND_TOOLBOX
    from agento.framework.config_test import tester_for_field as _tester_for_field

    ref = _tester_for_field("jira/jira_token")
    assert ref is not None
    assert ref.kind == KIND_TOOLBOX
    assert ref.label == "http"
