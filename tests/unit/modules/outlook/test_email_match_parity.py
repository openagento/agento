"""The Python inbound gate and the JS toolbox gate must agree, case for case.

Both read ``tests/fixtures/email_match_parity.json``. A divergence between the two is
fail-open on whichever side is more permissive, so the table lives in one file and each
language asserts against it. The JS half is
``src/agento/toolbox/tests/email-match.test.js``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.modules.outlook.src.channel import _matches_allowed

_FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "email_match_parity.json"
_CASES = json.loads(_FIXTURE.read_text())["cases"]


@pytest.mark.parametrize(
    "case", _CASES, ids=[f"{c['why']}" for c in _CASES]
)
def test_python_gate_matches_the_shared_parity_table(case):
    assert _matches_allowed(case["address"], case["patterns"]) is case["expected"]


def test_a_missing_address_never_raises():
    for bad in (None, "", "   "):
        assert _matches_allowed(bad, ["*@corp.com"]) is False
        assert _matches_allowed(bad, ["*"]) is False
