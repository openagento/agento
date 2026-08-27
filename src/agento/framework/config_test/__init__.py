"""Config-field testers — the "Test connection" / "Validate key" extension point.

A field declares its own test in the module's ``system.json``; the probe runs
where the credential already lives (the toolbox for anything on the network, the
declaring module's own Python for a local check). See docs/config/testers.md.
"""
from __future__ import annotations

from .manifest import (
    enumerate_test_groups,
    enumerate_testable_fields,
    field_schema_for_path,
    tester_for_field,
)
from .protocols import (
    CODE_RE,
    ERROR,
    FAIL,
    KIND_LOCAL,
    KIND_TOOLBOX,
    KINDS,
    NOT_CONFIGURED,
    OK,
    STATUSES,
    LocalTester,
    TesterRef,
    TestResult,
)
from .runner import run_config_test, sanitize

__all__ = [
    "CODE_RE", "ERROR", "FAIL", "KINDS", "KIND_LOCAL", "KIND_TOOLBOX",
    "NOT_CONFIGURED", "OK", "STATUSES", "LocalTester", "TestResult", "TesterRef",
    "enumerate_test_groups", "enumerate_testable_fields",
    "field_schema_for_path", "run_config_test", "sanitize", "tester_for_field",
]
