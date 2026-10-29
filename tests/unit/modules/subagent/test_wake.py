from __future__ import annotations

from unittest.mock import patch

import pytest

from agento.framework.events import JobRetryingEvent
from agento.modules.subagent.src.observers import (
    WakeParentObserver,
    _ancestors,
    _WakeConfig,
    maybe_wake,
    wake_key,
    wake_prompt,
)


def _row(id_, status, line_id=None, view="reviewer"):
    return {"id": id_, "status": status, "line_id": line_id, "view_code": view}


def test_the_prompt_names_each_child_line_by_its_latest_turn():
    prompt = wake_prompt([_row(101, "SUCCESS"), _row(102, "DEAD", view="tester"),
                          _row(105, "SUCCESS", line_id=101), _row(103, "FAILED", view=None)])
    assert prompt.splitlines() == [
        "Sub-agents you started have finished:",
        "#102 (tester) DEAD",
        "#103 (deleted view) FAILED",
        "#105 (reviewer) SUCCESS",
        "Read each result with agent_result.",
    ]


def test_the_wake_key_names_the_woken_row():
    assert wake_key(42) == "subagent_wake:42"


def test_a_retry_is_not_a_terminal_outcome_and_opens_no_connection():
    with patch("agento.modules.subagent.src.observers.pooled") as pooled:
        WakeParentObserver().execute(JobRetryingEvent(job=object(), error=RuntimeError(),  # type: ignore[arg-type]
                                                      delay_seconds=1))
    pooled.assert_not_called()


@pytest.mark.parametrize(("value", "on"), [
    (None, False), ("", False), ("0", False), (0, False), ("no", False),
    ("1", True), (1, True), ("true", True), (" YES ", True),
])
def test_wake_parent_parses_as_agent_js_truthy_does(value, on):
    """The twin of `truthy` in toolbox/agent.js: the description the agent reads and the
    wake must agree."""
    with patch("agento.modules.subagent.src.observers.ScopedConfigService") as svc:
        svc.return_value.get.return_value = value
        assert _WakeConfig(conn=None).on(7) is on


class _Cursor:
    """Records each statement and answers it with the next canned result."""

    def __init__(self, results: list[list[dict]]) -> None:
        self.sql: list[str] = []
        self._results: list[list[dict]] = results
        self._last: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def execute(self, sql, args=()) -> None:
        self.sql.append(sql)
        self._last = self._results.pop(0) if self._results else []

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


class _Conn:
    def __init__(self, cur: _Cursor) -> None:
        self._cur = cur

    def cursor(self):
        return self._cur

    def rollback(self) -> None:
        return None


class _AllWake:
    def wakeable(self, row) -> bool:
        return True


def _job(id_, parent, depth):
    return {"id": id_, "parent_job_id": parent, "status": "SUCCESS", "agent_view_id": 1,
            "line_id": None, "source": "jira", "reference_id": "AI-1", "priority": 50,
            "thread_id": 9, "depth": depth, "view_code": "v"}


def test_every_subtree_query_of_a_wake_stays_in_the_thread():
    """Class guard: each read of P's subtree (what P waits for and names) is bound to P's
    thread, so a detached row is neither waited for nor named."""
    down = _Cursor([[_job(1, None, 0)], [_job(2, 1, 1)], [_job(3, 2, 2)], [_job(4, 3, 3)], []])
    assert maybe_wake(_Conn(down), 1, _AllWake()) is None  # type: ignore[arg-type]
    walks = [s for s in down.sql if "parent_job_id =" in s or "parent_job_id IN" in s]
    assert len(walks) == 3
    assert [s for s in walks if "thread_id = %s" not in s] == []


def test_the_walk_up_crosses_a_detach():
    """The walk up is not thread-bound: the end of a detached row can be the last event its
    old parent (and every row above it) waits for."""
    up = _Cursor([[{"parent_job_id": 2}], [{"parent_job_id": 1}]])
    assert _ancestors(_Conn(up), 4, 3, 3) == [4, 3, 2, 1]
    assert [s for s in up.sql if "thread_id" in s] == []


@pytest.mark.parametrize("dry_run", [0, 1])
def test_the_wake_copies_the_parent_dry_run(dry_run):
    parent = {**_job(1, None, 0), "dry_run": dry_run}
    cur = _Cursor([[parent], [_job(2, 1, 1)], []])
    with patch("agento.modules.subagent.src.observers.publish_job", return_value=77) as publish:
        assert maybe_wake(_Conn(cur), 1, _AllWake()) == 77  # type: ignore[arg-type]
    assert "j.dry_run" in cur.sql[0]
    assert publish.call_args.kwargs["dry_run"] is bool(dry_run)
