"""The ``--test`` JSON: validation and the stored form (framework/test_run.py)."""
from __future__ import annotations

import pytest

from agento.framework.retry_policy import NON_RETRYABLE_ERRORS
from agento.framework.test_run import (
    MAX_TEST_CONFIG_BYTES,
    TEST_MODE_ENV,
    InvalidTestConfig,
    is_test_mode_enabled,
    load_test_config,
    parse_test_config,
    require_test_mode,
)


@pytest.fixture(autouse=True)
def enable_test_mode(monkeypatch):
    monkeypatch.setenv(TEST_MODE_ENV, "1")


@pytest.mark.parametrize(("value", "enabled"), [
    ("1", True), (None, False), ("", False), ("0", False), ("true", False),
])
def test_test_mode_is_on_only_for_an_explicit_1(monkeypatch, value, enabled):
    if value is None:
        monkeypatch.delenv(TEST_MODE_ENV)
    else:
        monkeypatch.setenv(TEST_MODE_ENV, value)
    assert is_test_mode_enabled() is enabled


def test_with_test_mode_off_a_config_is_refused_and_a_job_is_not_retried(monkeypatch):
    monkeypatch.delenv(TEST_MODE_ENV)
    with pytest.raises(InvalidTestConfig, match="test mode is off"):
        parse_test_config("{}")
    with pytest.raises(ValueError, match=TEST_MODE_ENV) as exc:
        require_test_mode()
    # The consumer reads the class name: a plain ValueError fails the job at once.
    assert type(exc.value).__name__ in NON_RETRYABLE_ERRORS


@pytest.mark.parametrize(("text", "parsed"), [
    ("{}", {"stub": [], "real": []}),
    ('{"stub": ["jira_search", "jira_search"], "real": ["jira_add_comment"]}',
     {"stub": ["jira_search"], "real": ["jira_add_comment"]}),
])
def test_a_valid_config_is_returned_in_canonical_form(text, parsed):
    assert parse_test_config(text) == parsed


@pytest.mark.parametrize(("text", "message"), [
    ("{not json", "not valid JSON"),
    ("[]", "must be a JSON object"),
    ('{"stub": [], "ssh": true}', "unknown keys: ssh"),
    ('{"stub": "jira_search"}', "'stub' must be a list of tool names"),
    ('{"real": [1]}', "'real' must be a list of tool names"),
    ('{"stub": ["jira_search"], "real": ["jira_search"]}', "both 'stub' and 'real': jira_search"),
    ('{"stub": ["no_such_tool"]}', "no module declares: no_such_tool"),
    ('{"stub": ["' + "x" * MAX_TEST_CONFIG_BYTES + '"]}', "over"),
], ids=["json", "object", "key", "stub-list", "real-list", "both", "undeclared", "size"])
def test_an_invalid_config_is_refused(text, message):
    with pytest.raises(InvalidTestConfig, match=message):
        parse_test_config(text)


@pytest.mark.parametrize(("stored", "loaded"), [
    (None, None),
    ('{"stub": [], "real": []}', {"stub": [], "real": []}),
    ({"stub": ["a"], "real": []}, {"stub": ["a"], "real": []}),
    ("null", None),
])
def test_a_stored_column_loads_as_a_dict_or_none(stored, loaded):
    assert load_test_config(stored) == loaded
