"""The shared RRULE vectors (AG-67 plan §10, S-3): the engine side. The Node side is
src/agento/toolbox/tests/scheduler-tools.test.js; both read the same file."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from agento.framework.schedule import engine

FIXTURE = json.loads((Path(__file__).parents[3] / "fixtures/scheduler/rrule_vectors.json").read_text())
START = datetime(2027, 1, 1, 9, 0)


@pytest.mark.parametrize("vector", FIXTURE["vectors"], ids=lambda v: v["rrule"] or "<empty>")
def test_the_engine_answers_each_vector(vector):
    try:
        engine.validate(vector["rrule"], START, "UTC", min_interval_minutes=FIXTURE["min_interval_minutes"],
                        now=datetime(2027, 1, 1, 8, 0))
        ok = True
    except engine.InvalidSchedule:
        ok = False
    assert ok is vector["python_ok"]


def test_node_never_refuses_what_the_engine_accepts():
    assert [v["rrule"] for v in FIXTURE["vectors"] if not v["node_ok"] and v["python_ok"]] == []
