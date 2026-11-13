"""The schedule_* tools under the toolbox's real grants (AG-67 plan §10, §11 step 11): the real
handlers (tests/integration/node/subagent_tool.mjs) connect as a user with exactly
db_grants.TOOLBOX_GRANTS; the tick and the store run as the backend."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from agento.framework.access import accounts
from agento.framework.channels.registry import register_channel
from agento.framework.job_types import register_job_type
from agento.framework.schedule import sources, tick
from agento.modules.scheduler.src import store
from agento.modules.scheduler.src.channel import SchedulerChannel
from agento.modules.scheduler.src.source import DELETED_SUFFIX, SchedulerSource

from .test_subagent_flow import _run, pytestmark, toolbox_db_user  # noqa: F401  (real-grants user)

CONFIGS = {"core": {"scheduler/min_interval_minutes": "5", "scheduler/default_timezone": "UTC"},
           "scheduler": {"limits/max_per_user": "10", "limits/max_per_agent_view": "20"},
           "conversation": {"limits/max_message_bytes": "32768"}}


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    monkeypatch.setattr(sources, "load_sources", lambda: {"scheduler": SchedulerSource()})
    register_job_type("scheduler", module="scheduler")
    register_channel(SchedulerChannel())


def _job(conn, view, requester_key=None, trust="claimed") -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO job (type, source, agent_view_id, idempotency_key, requester_key, requester_trust,"
                    " status) VALUES ('blank', 'jira', %s, %s, %s, %s, 'RUNNING')",
                    (view, f"caller:{view}:{requester_key}", requester_key, trust))
        job_id = cur.lastrowid
    conn.commit()
    return job_id


def _tool(tool, args, job_id, view):
    [res] = _run({"tool": tool, "args": args, "jobId": job_id, "agentViewId": view, "moduleConfigs": CONFIGS})
    return res


def _ok(tool, args, job_id, view):
    res = _tool(tool, args, job_id, view)
    assert not res["isError"], res["text"]
    return json.loads(res["text"])


def _one(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
    conn.rollback()
    return row


def test_create_list_and_a_soft_delete_the_tick_finalizes(conn, world):
    job = _job(conn, world["view"])
    sid = _ok("schedule_create", {"title": "Digest", "prompt": "Summarize.", "rrule": "FREQ=HOURLY;BYMINUTE=0"},
              job, world["view"])["id"]
    row = _one(conn, "SELECT * FROM schedule WHERE id = %s", (sid,))
    assert (row["owner_agent_view_id"], row["owner_user_id"], row["created_job_id"], row["next_fire_at"]) == (
        world["view"], None, job, None)
    assert _one(conn, "SELECT actor FROM schedule_audit WHERE schedule_id = %s", (sid,))["actor"] == f"job:{job}"
    # The run made a conversation; the tool deletes; the tick (phase A) retitles and removes.
    _, _, conv_id = store.run_now(conn, sid, user=world["admin"], actor="test")
    assert _ok("schedule_delete", {"id": sid}, job, world["view"]) == {"id": sid, "deleted": True}
    assert _one(conn, "SELECT deleted_at FROM schedule WHERE id = %s", (sid,))["deleted_at"] is not None
    assert _ok("schedule_list", {}, job, world["view"]) == []
    tick.tick(conn, now=datetime.now(UTC), batch=10)
    assert _one(conn, "SELECT 1 FROM schedule WHERE id = %s", (sid,)) is None
    assert _one(conn, "SELECT title FROM conversation WHERE id = %s", (conv_id,))["title"] == "Digest" + DELETED_SUFFIX


def test_an_account_job_creates_a_user_owned_schedule(conn, world):
    accounts.add_grant(conn, "user", "operation", "scheduler.own")
    job = _job(conn, world["view"], f"user:{world['owner'].id}", "account")
    sid = _ok("schedule_create", {"title": "t", "prompt": "p", "rrule": "FREQ=DAILY"}, job, world["view"])["id"]
    assert _one(conn, "SELECT owner_user_id, owner_agent_view_id FROM schedule WHERE id = %s", (sid,)) == {
        "owner_user_id": world["owner"].id, "owner_agent_view_id": None}
    listed = _ok("schedule_list", {}, job, world["view"])
    assert [(r["id"], r["owner"], r["editable"]) for r in listed] == [(sid, "user", True)]
    # Another view's job does not reach it (OP-1).
    other = _job(conn, world["other_view"])
    assert _tool("schedule_update", {"id": sid, "title": "x"}, other, world["other_view"])["text"] == (
        "schedule not found")


def test_a_jira_row_is_listed_but_not_writable(conn, world):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO schedule (source, source_ref, title, agent_view_id, owner_agent_view_id, rrule,"
                    " starts_at, timezone) VALUES ('jira_periodic', 'AB-1', 'j', %s, %s, 'FREQ=DAILY',"
                    " '2026-01-01 00:00:00', 'UTC')", (world["view"], world["view"]))
        sid = cur.lastrowid
    conn.commit()
    job = _job(conn, world["view"])
    assert [(r["id"], r["editable"]) for r in _ok("schedule_list", {}, job, world["view"])] == [(sid, False)]
    for tool, args in (("schedule_update", {"id": sid, "title": "x"}), ("schedule_delete", {"id": sid})):
        res = _tool(tool, args, job, world["view"])
        assert res["isError"] and "read-only" in res["text"]
    assert _one(conn, "SELECT title, deleted_at FROM schedule WHERE id = %s", (sid,)) == {"title": "j", "deleted_at": None}


def test_a_tool_start_change_resets_the_anchor(conn, world):
    """The tool writer of the anchor-reset test (the panel one is in test_scheduler_module.py)."""
    job = _job(conn, world["view"])
    sid = _ok("schedule_create", {"title": "t", "prompt": "p", "rrule": "FREQ=DAILY",
                                  "starts_at": "2027-01-01T09:00", "timezone": "UTC"}, job, world["view"])["id"]
    tick.tick(conn, now=datetime(2026, 12, 31, 12, 0), batch=10)  # fills next_fire_at
    for day in (1, 2):
        assert tick.tick(conn, now=datetime(2027, 1, day, 9, 0), batch=10).published == 1
        with conn.cursor() as cur:
            cur.execute("UPDATE job SET status = 'SUCCESS' WHERE schedule_id = %s", (sid,))
        conn.commit()
    _ok("schedule_update", {"id": sid, "starts_at": "2027-01-01T10:00"}, job, world["view"])
    assert _one(conn, "SELECT next_fire_at, anchor_local FROM schedule WHERE id = %s", (sid,)) == {
        "next_fire_at": None, "anchor_local": None}
    tick.tick(conn, now=datetime(2027, 1, 2, 12, 0), batch=10)  # fills from the new start
    assert tick.tick(conn, now=datetime(2027, 1, 3, 10, 0), batch=10).published == 1
    row = _one(conn, "SELECT last_fire_at, next_fire_at FROM schedule WHERE id = %s", (sid,))
    assert row == {"last_fire_at": datetime(2027, 1, 3, 10, 0), "next_fire_at": datetime(2027, 1, 4, 10, 0)}


def test_a_job_with_no_account_user_does_not_reach_another_views_row(conn, world):
    """OP-1: a NULL account user matches no row (`=`, never `<=>`)."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO schedule (source, title, prompt, agent_view_id, owner_agent_view_id, rrule,"
                    " starts_at, timezone) VALUES ('scheduler', 'theirs', 'p', %s, %s, 'FREQ=DAILY',"
                    " '2026-01-01 00:00:00', 'UTC')", (world["view"], world["other_view"]))
        sid = cur.lastrowid
    conn.commit()
    job = _job(conn, world["view"])
    assert _ok("schedule_list", {}, job, world["view"]) == []
    for tool, args in (("schedule_update", {"id": sid, "title": "x"}), ("schedule_delete", {"id": sid})):
        assert _tool(tool, args, job, world["view"])["text"] == "schedule not found"
