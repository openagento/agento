"""Test runs: which tools one run stubs, given as JSON (DECISIONS.md D-TEST-RUN-1).

The test framework owns the configuration and hands Agento the result for one job:

    {"stub": ["jira_search"], "real": ["jira_add_comment"]}

A tool in ``stub`` is not run; a tool in ``real`` runs. A tool in neither is stubbed when its
module marks it ``tools/<name>/sensitive`` = 1. A test run never gets the SSH private key.
Test mode exists only where ``AGENTO_TEST_MODE_ENABLED=1``; it is off by default, so a
production deployment refuses it.
An unknown key is an error, so a newer option never silently does nothing. The JSON travels
in argv: it must never carry a secret.
"""
from __future__ import annotations

import json
import os

TEST_MODE_ENV = "AGENTO_TEST_MODE_ENABLED"
MAX_TEST_CONFIG_BYTES = 16384
_KEYS = ("stub", "real")


class InvalidTestConfig(ValueError):
    """A ``--test`` value that is not a valid test configuration."""


_TEST_MODE_OFF = f"test mode is off in this deployment (set {TEST_MODE_ENV}=1 to allow it)"


def is_test_mode_enabled() -> bool:
    """Whether this deployment allows test runs: only an explicit ``1``."""
    return os.environ.get(TEST_MODE_ENV) == "1"


def require_test_mode() -> None:
    """Refuse a test run where test mode is off (a plain ValueError: a job is not retried)."""
    if not is_test_mode_enabled():
        raise ValueError(_TEST_MODE_OFF)


def parse_test_config(text: str) -> dict:
    """Validate the ``--test`` JSON and return it in canonical form."""
    from .tool_enablement import declared_tool_names

    if not is_test_mode_enabled():
        raise InvalidTestConfig(_TEST_MODE_OFF)
    if len(text.encode("utf-8")) > MAX_TEST_CONFIG_BYTES:
        raise InvalidTestConfig(f"--test is over {MAX_TEST_CONFIG_BYTES} bytes")
    try:
        config = json.loads(text)
    except ValueError:
        raise InvalidTestConfig("--test is not valid JSON") from None
    if not isinstance(config, dict):
        raise InvalidTestConfig("--test must be a JSON object")
    unknown = sorted(set(config) - set(_KEYS))
    if unknown:
        raise InvalidTestConfig(f"--test has unknown keys: {', '.join(unknown)}")

    lists = {}
    for key in _KEYS:
        names = config.get(key, [])
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise InvalidTestConfig(f"--test '{key}' must be a list of tool names")
        lists[key] = sorted(set(names))
    both = sorted(set(lists["stub"]) & set(lists["real"]))
    if both:
        raise InvalidTestConfig(f"--test names tools in both 'stub' and 'real': {', '.join(both)}")
    undeclared = sorted((set(lists["stub"]) | set(lists["real"])) - declared_tool_names())
    if undeclared:
        raise InvalidTestConfig(f"--test names tools no module declares: {', '.join(undeclared)}")
    return lists


def load_test_config(value) -> dict | None:
    """A stored ``test_config`` column (JSON text or an already parsed value) as a dict."""
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else None
