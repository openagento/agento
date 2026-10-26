"""Every job in a thread, and who may read it (E9 §3.4-§3.7).

A conversation is 1:1 with a framework thread. A panel job runs in its conversation's thread.
Any other job (Jira, Outlook, a cron) runs in its thread's **channel conversation**, with no
owner, read-only, and readable by an admin only.
"""
from __future__ import annotations

import ast
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agento.framework.access import accounts
from agento.framework.execution_hooks import DeltaRecord
from agento.framework.threads import default_ref, ensure_thread
from agento.modules.conversation.src import retention, routes, service, stream
from agento.modules.conversation.src.deltas import ConversationDeltaSink
from agento.modules.conversation.src.finalizer import ConversationFinalizer
from agento.modules.conversation.src.hooks import ConversationExecutionIds

from .conftest import _test_connection
from .test_conversation_events import seen  # noqa: F401
from .test_conversation_submission import _Req


@pytest.fixture(autouse=True)
def _runs(conn):
    yield
    with conn.cursor() as cur:
        cur.execute("DELETE FROM execution_delta")
        cur.execute("DELETE FROM execution")
        cur.execute("DELETE FROM conversation_prune_watermark")
    conn.commit()


def _job(conn, *, source="jira", reference="AI-7", view=None, prompt=None, output=None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job (type, source, agent_view_id, reference_id, idempotency_key, "
            "                 status, attempt, max_attempts, prompt, output) "
            "VALUES ('cron', %s, %s, %s, %s, 'RUNNING', 1, 3, %s, %s)",
            (source, view, reference, str(uuid.uuid4()), prompt, output))
        job_id = cur.lastrowid
        # The thread the claim gives a job that has none (`threads.place_claim`).
        cur.execute("UPDATE job SET thread_id = %s WHERE id = %s",
                    (ensure_thread(cur, agent_view_id=view,
                                   ref=default_ref(source, reference, job_id)), job_id))
    conn.commit()
    return job_id


def _claim(conn, job_id: int) -> str:
    """What the consumer does at claim: mint the run, in the claim's transaction."""
    execution_id = ConversationExecutionIds().mint(conn=conn, job_id=job_id, attempt=1)
    conn.commit()
    return execution_id


def _rows(conn, sql, args=()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = list(cur.fetchall())
    conn.commit()
    return rows


def _thread_of(conn, execution_id: str) -> dict:
    return _rows(conn, "SELECT c.* FROM conversation c JOIN execution e "
                       "ON e.conversation_id = c.id WHERE e.execution_id = %s",
                 (execution_id,))[0]


def _kinds(conn, conversation_id: int) -> list[str]:
    return [r["kind"] for r in _rows(
        conn, "SELECT kind FROM conversation_event WHERE conversation_id = %s ORDER BY id",
        (conversation_id,))]


# --- linking (P5) -------------------------------------------------------------

def test_a_jira_job_gets_an_ownerless_channel_thread_with_its_run(conn, world):
    execution_id = _claim(conn, _job(conn, view=world["view"]))

    thread = _thread_of(conn, execution_id)
    assert (thread["user_id"], thread["channel"], thread["external_ref"]) == (None, "jira", "AI-7")
    assert thread["agent_view_id"] == world["view"]
    assert _kinds(conn, thread["id"]) == ["run.started"]


def test_run_started_says_which_attempt_of_how_many(conn, world):
    """The panel shows "Attempt n of m" on a failed turn (E9 chat UX, U4)."""
    execution_id = _claim(conn, _job(conn, view=world["view"]))

    payload = json.loads(_rows(conn, "SELECT payload FROM conversation_event "
                                     "WHERE execution_id = %s", (execution_id,))[0]["payload"])
    assert (payload["attempt"], payload["max_attempts"]) == (1, 3)


def test_the_next_job_on_the_same_issue_and_a_follow_up_reuse_the_thread(conn, world):
    """A follow-up copies its parent's source and reference (`schedule.js`), so the same
    key puts it in the same thread - no special case."""
    first = _thread_of(conn, _claim(conn, _job(conn, view=world["view"])))
    again = _thread_of(conn, _claim(conn, _job(conn, view=world["view"])))
    other_issue = _thread_of(conn, _claim(conn, _job(conn, view=world["view"],
                                                     reference="AI-8")))

    assert again["id"] == first["id"]
    assert other_issue["id"] != first["id"]
    assert _kinds(conn, first["id"]) == ["run.started", "run.started"]


def test_an_archived_channel_thread_is_reactivated_by_its_next_run(conn, world):
    thread = _thread_of(conn, _claim(conn, _job(conn, view=world["view"])))
    service.archive(conn, thread["id"], actor_id=None, reason="idle")

    again = _thread_of(conn, _claim(conn, _job(conn, view=world["view"])))

    assert again["id"] == thread["id"]
    assert again["status"] == "active"


def test_a_job_with_no_view_and_a_job_with_no_reference_each_get_their_own_thread(conn, world):
    with_view = _thread_of(conn, _claim(conn, _job(conn, view=world["view"])))
    no_view = _thread_of(conn, _claim(conn, _job(conn, view=None)))
    no_ref_a = _thread_of(conn, _claim(conn, _job(conn, source="cron", reference=None)))
    no_ref_b = _thread_of(conn, _claim(conn, _job(conn, source="cron", reference=None)))

    assert no_view["id"] != with_view["id"]
    assert no_view["agent_view_id"] is None
    assert no_ref_a["id"] != no_ref_b["id"]


def test_a_panel_job_runs_in_the_thread_of_its_message(conn, world):
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    _, job_id, _ = service.submit_message(conn, conversation_id=conversation_id,
                                          user_id=world["owner"].id, client_message_id="m1",
                                          content="hello")

    assert _thread_of(conn, _claim(conn, job_id))["id"] == conversation_id
    assert _kinds(conn, conversation_id) == ["message.created", "run.started"]


def test_the_deltas_and_the_answer_of_a_channel_run_reach_its_thread(conn, world, monkeypatch):
    values = {"stream/max_deltas_per_execution": 100,
              "stream/max_delta_bytes_per_execution": 1 << 20,
              "stream/max_fragment_bytes": 8192, "limits/max_message_bytes": 65536}
    monkeypatch.setattr(service, "config", lambda conn, path: values[path])
    job_id = _job(conn, view=world["view"], output="Done: **AI-7** fixed.")
    execution_id = _claim(conn, job_id)
    sink = ConversationDeltaSink()
    try:
        sink.write([DeltaRecord(execution_id=execution_id, seq=1, kind="tool.started",
                                text="", tool_name="jira_get_issue",
                                data={"call_id": "c1", "input": '{"key": "AI-7"}'})])
    finally:
        if sink._conn is not None:
            sink._conn.close()

    ConversationFinalizer().finalize(conn=conn, job_id=job_id, attempt=1,
                                     execution_id=execution_id, outcome="succeeded",
                                     job_terminal=True)
    conn.commit()

    thread = _thread_of(conn, execution_id)
    assert _kinds(conn, thread["id"]) == ["run.started", "tool.started", "run.finished",
                                          "assistant.message"]
    answer = _rows(conn, "SELECT payload FROM conversation_event WHERE kind = "
                         "'assistant.message' AND conversation_id = %s", (thread["id"],))[0]
    assert json.loads(answer["payload"])["content"] == "Done: **AI-7** fixed."


def test_a_claude_run_leaves_its_answer_text_and_no_stream_json(conn, world):
    """P1 end to end: the parser's `raw_output` is what the consumer stores as
    `job.output`, and that is the answer bubble - the `result` text, not the JSONL."""
    from agento.modules.claude.src.output_parser import parse_claude_output

    stream_json = (Path(__file__).resolve().parents[1] / "fixtures" / "claude"
                   / "stream_tool_use.jsonl").read_text()
    job_id = _job(conn, view=world["view"], output=parse_claude_output(stream_json).raw_output)
    execution_id = _claim(conn, job_id)

    ConversationFinalizer().finalize(conn=conn, job_id=job_id, attempt=1,
                                     execution_id=execution_id, outcome="succeeded",
                                     job_terminal=True)
    conn.commit()

    answer = _rows(conn, "SELECT content FROM message WHERE execution_id = %s",
                   (execution_id,))[0]["content"]
    assert answer == "The ticket asks to fix the login page.\nNo local files exist yet."


def test_a_failed_channel_run_is_closed_without_an_answer(conn, world):
    job_id = _job(conn, view=world["view"], output="partial")
    execution_id = _claim(conn, job_id)

    ConversationFinalizer().finalize(conn=conn, job_id=job_id, attempt=1,
                                     execution_id=execution_id, outcome="failed",
                                     job_terminal=True)
    conn.commit()

    thread = _thread_of(conn, execution_id)
    assert _kinds(conn, thread["id"]) == ["run.started", "run.finished"]


# --- the channel idle clock ------------------------------------------------------

def test_an_idle_channel_thread_is_archived_on_its_activity_clock(conn, world):
    execution_id = _claim(conn, _job(conn, view=world["view"]))
    thread = _thread_of(conn, execution_id)
    with conn.cursor() as cur:
        cur.execute("UPDATE execution SET status = 'succeeded' WHERE execution_id = %s",
                    (execution_id,))
        cur.execute("UPDATE conversation SET last_activity_at = NOW() - INTERVAL 40 DAY, "
                    "created_at = NOW() - INTERVAL 40 DAY WHERE id = %s", (thread["id"],))
    conn.commit()

    assert retention.auto_archive(conn, idle_days=30) == 1
    assert _thread_of(conn, execution_id)["status"] == "archived"


def test_an_old_answer_followed_by_new_run_events_is_not_idle(conn, world):
    """An assistant row does not move the clock back: the later run's events count."""
    job_id = _job(conn, view=world["view"], output="old answer")
    execution_id = _claim(conn, job_id)
    ConversationFinalizer().finalize(conn=conn, job_id=job_id, attempt=1,
                                     execution_id=execution_id, outcome="succeeded",
                                     job_terminal=True)
    thread = _thread_of(conn, execution_id)
    with conn.cursor() as cur:
        cur.execute("UPDATE message SET created_at = NOW() - INTERVAL 40 DAY "
                    "WHERE conversation_id = %s", (thread["id"],))
        cur.execute("UPDATE conversation SET created_at = NOW() - INTERVAL 40 DAY "
                    "WHERE id = %s", (thread["id"],))
    conn.commit()

    assert retention.auto_archive(conn, idle_days=30) == 0


def test_a_channel_thread_with_a_running_run_is_not_idle(conn, world):
    execution_id = _claim(conn, _job(conn, view=world["view"]))
    thread = _thread_of(conn, execution_id)
    with conn.cursor() as cur:
        cur.execute("UPDATE conversation SET last_activity_at = NOW() - INTERVAL 40 DAY, "
                    "created_at = NOW() - INTERVAL 40 DAY WHERE id = %s", (thread["id"],))
    conn.commit()

    assert retention.auto_archive(conn, idle_days=30) == 0


def test_a_thread_delete_removes_its_runs_and_their_ledger(conn, world):
    execution_id = _claim(conn, _job(conn, view=world["view"]))
    thread = _thread_of(conn, execution_id)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO execution_delta (execution_id, seq, kind) "
                    "VALUES (%s, 1, 'assistant.text')", (execution_id,))
    conn.commit()

    assert service.delete_conversation(conn, thread["id"])
    assert _rows(conn, "SELECT 1 FROM execution WHERE execution_id = %s", (execution_id,)) == []
    assert _rows(conn, "SELECT 1 FROM execution_delta WHERE execution_id = %s",
                 (execution_id,)) == []


# --- who sees what (SEC-7) ---------------------------------------------------------

def _channel_thread(conn, world) -> int:
    return _thread_of(conn, _claim(conn, _job(conn, view=world["view"])))["id"]


def test_a_non_admin_gets_404_on_every_read_of_a_channel_thread(conn, world):
    """404, never 403: a 403 would confirm the thread exists."""
    cid = _channel_thread(conn, world)
    user = world["owner"]

    for handler in (routes.show, routes.timeline, routes.events, routes.messages,
                    stream.open_stream):
        assert handler(_Req(conn, user, params={"id": cid})).status == 404, handler
    listed = routes.index(_Req(conn, user, query={"scope": "channels"}))
    assert (listed.status, listed.body) == (200, [])


def test_an_admin_lists_and_reads_a_channel_thread(conn, world):
    cid = _channel_thread(conn, world)
    admin = world["admin"]

    listed = routes.index(_Req(conn, admin, query={"scope": "channels", "channel": "jira"}))
    assert [t["id"] for t in listed.body] == [cid]
    assert listed.body[0]["live"] is True                 # its run is still running
    shown = routes.show(_Req(conn, admin, params={"id": cid}))
    assert shown.status == 200 and len(shown.body["runs"]) == 1
    assert shown.body["run_details"] is True and "model" in shown.body["runs"][0]
    assert shown.body["channel_write"] is True            # admin holds every operation
    page = routes.timeline(_Req(conn, admin, params={"id": cid}))
    assert [e["kind"] for e in page.body["events"]] == ["run.started"]


# --- writing into a channel thread (ROADMAP E9, 2026-10-10) ------------------------

def _reply(conn, user, cid, *, content="odpowiedź", client_message_id="r1"):
    return routes.send(_Req(conn, user, params={"id": str(cid)},
                            json={"client_message_id": client_message_id, "content": content}))


def _job_row(conn, job_id) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM job WHERE id = %s", (job_id,))
        row = cur.fetchone()
    conn.commit()
    return row


def test_an_admin_reply_publishes_a_followup_on_the_threads_own_source(conn, world):
    cid = _channel_thread(conn, world)

    sent = _reply(conn, world["admin"], cid)

    assert sent.status == 201
    job = _job_row(conn, sent.body["job_id"])
    # The reply continues the EXTERNAL task: the thread's own source and reference, with
    # the operator's text where `FollowupWorkflow` reads it.
    assert (job["type"], job["source"], job["reference_id"]) == ("followup", "jira", "AI-7")
    assert job["context"] == "odpowiedź" and job["prompt"] is None
    assert job["idempotency_key"].startswith("channel-reply:")


def test_the_followup_workflow_can_execute_the_published_reply(conn, world):
    """F9: `execute_job` raises unless the job carries `context`."""
    from agento.framework.job_models import Job
    from agento.framework.workflows.followup import FollowupWorkflow

    cid = _channel_thread(conn, world)
    row = _job_row(conn, _reply(conn, world["admin"], cid).body["job_id"])
    job = Job.from_row(row)

    workflow = FollowupWorkflow(runner=None, logger=logging.getLogger(__name__))
    built = workflow.build_prompt(_FakeChannel(), job.reference_id,
                                  instructions=job.context, config=None)

    assert "odpowiedź" in built


class _FakeChannel:
    name = "jira"

    def get_followup_fragments(self, reference_id, instructions, config=None):
        from agento.framework.channels.base import PromptFragments
        return PromptFragments(read_context="r", respond="w", extra=instructions)


def test_a_plain_user_cannot_reply(conn, world):
    """404, not 403: a channel thread is unreadable to a non-admin, so the reach gate
    refuses before the grant is consulted. `conversation.channel_write` is the second
    gate, and the one that decides once channel reads open to a role (ROADMAP E9)."""
    cid = _channel_thread(conn, world)

    assert _reply(conn, world["owner"], cid).status == 404
    assert service.can_write_channel(
        conn, world["owner"],
        {"agent_view_id": world["view"], "workspace_id": world["workspace"]}) is False


def test_a_reply_is_refused_when_the_thread_has_nothing_to_reply_to(conn, world):
    cid = _channel_thread(conn, world)
    with conn.cursor() as cur:
        cur.execute("UPDATE conversation SET external_ref = NULL WHERE id = %s", (cid,))
    conn.commit()

    sent = _reply(conn, world["admin"], cid)

    assert (sent.status, sent.body) == (409, {"error": "read_only"})


def test_a_repeated_reply_yields_one_job_and_one_message(conn, world):
    cid = _channel_thread(conn, world)

    first = _reply(conn, world["admin"], cid)
    again = _reply(conn, world["admin"], cid)

    assert (first.body["job_id"], again.status) == (again.body["job_id"], 200)
    assert len(_rows(conn, "SELECT id FROM message WHERE conversation_id = %s", (cid,))) == 1


def test_a_crash_before_the_publish_is_recovered_on_the_channel_contract(conn, world):
    """The sweep must not re-publish a channel reply as a panel `conversation` job."""
    cid = _channel_thread(conn, world)
    message_id, _, _ = service.submit_message(
        conn, conversation_id=cid, user_id=world["admin"].id,
        client_message_id="crash-1", content="odpowiedź po crashu")
    # Back to the state a crash between the two commits leaves behind.
    with conn.cursor() as cur:
        cur.execute("UPDATE message SET job_state = 'pending', job_id = NULL WHERE id = %s",
                    (message_id,))
        cur.execute("DELETE FROM job WHERE idempotency_key LIKE 'channel-reply:%%'")
    conn.commit()

    assert service.sweep_pending(conn, grace_seconds=0) == 1

    rows = _rows(conn, "SELECT job_id FROM message WHERE id = %s", (message_id,))
    job = _job_row(conn, rows[0]["job_id"])
    assert (job["type"], job["source"]) == ("followup", "jira")


@pytest.mark.parametrize(("outcome", "job_terminal", "job_state"), [
    pytest.param("succeeded", True, "terminal", id="finished-reply-is-terminal"),
    pytest.param("failed", False, "published", id="retried-reply-stays-published"),
])
def test_the_finalizer_sets_a_channel_reply_state(conn, world, outcome, job_terminal, job_state):
    cid = _channel_thread(conn, world)
    job_id = _reply(conn, world["admin"], cid).body["job_id"]
    execution_id = _claim(conn, job_id)   # the reply runs in the thread it was written in

    ConversationFinalizer().finalize(conn=conn, job_id=job_id, attempt=1,
                                     execution_id=execution_id, outcome=outcome,
                                     job_terminal=job_terminal)
    conn.commit()

    states = _rows(conn, "SELECT job_state FROM message WHERE job_id = %s", (job_id,))
    assert [r["job_state"] for r in states] == [job_state]


def test_the_channel_list_pages_by_its_cursor_without_overlap(conn, world, monkeypatch):
    ids = {_thread_of(conn, _claim(conn, _job(conn, reference=f"AI-{n}")))["id"]
           for n in range(5)}
    real = service.config
    monkeypatch.setattr(service, "config", lambda c, path: 2 if path == "history/page_size"
                        else real(c, path))
    listed, before = [], None
    for _ in range(4):
        query = {"scope": "channels", **({"before": before} if before else {})}
        page = routes.index(_Req(conn, world["admin"], query=query)).body
        if not page:
            break
        listed += [t["id"] for t in page]
        before = page[-1]["cursor"]

    assert sorted(listed) == sorted(ids) and len(listed) == len(set(listed))


def test_tool_payloads_are_an_admins_only(conn, world):
    """The name, the call id and the error flag stay; input and output go (E9 §3.6)."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    with conn.cursor() as cur:
        service.append_event(cur, cid, kind="tool.completed", source_kind="delta",
                             source_id=990001,
                             payload={"seq": 1, "text": "", "tool_name": "bash",
                                      "data": {"call_id": "c1", "input": "ls",
                                               "output": "secret.txt", "is_error": False}})
    conn.commit()

    def data(user):
        page = routes.timeline(_Req(conn, user, params={"id": cid})).body
        return page["events"][0]["payload"]["data"]

    assert data(world["owner"]) == {"call_id": "c1", "is_error": False}
    assert data(world["admin"]) == {"call_id": "c1", "input": "ls", "output": "secret.txt",
                                    "is_error": False}


# --- the timeline route (P6) ----------------------------------------------------

T0 = datetime(2026, 1, 5, 9, 0, 0)        # an imported thread's own past, not "now"


def _events(conn, cid: int, n: int) -> list[int]:
    out = []
    with conn.cursor() as cur:
        for i in range(n):
            out.append(service.append_event(cur, cid, kind="assistant.text",
                                             source_kind="delta", source_id=980000 + i,
                                             payload={"seq": i, "text": str(i)}))
    conn.commit()
    return out


def _timed(conn, cid: int, times: list[datetime]) -> list[int]:
    """One event per time, written in list order: a later entry has a higher id, whatever
    its time - what `conversation:backfill` does to a thread with native runs."""
    out = []
    with conn.cursor() as cur:
        for at in times:
            out.append(service.append_event(cur, cid, kind="assistant.text",
                                             source_kind="delta", source_id=len(out) + 960000,
                                             payload={}, created_at=at))
    conn.commit()
    return out


def _by_time(ids: list[int], times: list[datetime]) -> list[int]:
    return [i for _, i in sorted(zip(times, ids, strict=True))]


def _page(conn, user, cid: int, **query) -> dict:
    response = routes.timeline(_Req(conn, user, params={"id": cid}, query=query))
    assert response.status == 200, response.body
    return response.body


def _walk(conn, user, cid: int, *, limit: int, pair: bool = True,
          between=lambda oldest: None) -> list[list[int]]:
    """Every page, newest first, as the panel loads them: the cursor is the oldest event the
    client holds, from its own page. `between` runs after each page read."""
    pages, query = [], {"limit": str(limit)}
    for _ in range(20):                 # a cursor that does not move must fail, not hang
        body = _page(conn, user, cid, **query)
        pages.append([e["id"] for e in body["events"]])
        if not body["has_older"]:
            return pages
        oldest = body["events"][0]
        between(oldest)
        query = {"limit": str(limit), "before": str(oldest["id"]),
                 **({"before_at": oldest["created_at"]} if pair else {})}
    raise AssertionError(f"no last page after {pages}")


def _drop(conn, event_id: int) -> None:
    """What the finalizer does to a run's partial events: the row goes, no watermark moves."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM conversation_event WHERE id = %s", (event_id,))
    conn.commit()


@pytest.mark.parametrize("pair", [True, False], ids=["before_at", "bare_before"])
def test_the_timeline_pages_back_in_time_order_with_no_gap_and_no_overlap(conn, world, pair):
    """An imported event (old time, high id) sits in its time place, on whichever page that
    is. A bare `before` (the shipped form) pages the same as the pair while its event lives."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    times = [T0 + timedelta(seconds=s) for s in (1, 2, 3, 4, 5, 0.5, 2.5)]
    ids = _timed(conn, cid, times)
    order = _by_time(ids, times)

    pages = _walk(conn, world["owner"], cid, limit=3, pair=pair)

    assert pages == [order[4:], order[1:4], order[:1]]          # each page oldest first


@pytest.mark.parametrize("at", [T0, T0 + timedelta(microseconds=123456)],
                         ids=["whole_second", "microsecond"])
def test_a_tie_in_time_is_ordered_by_id_across_a_page_edge(conn, world, at):
    """Rows from before migration 008 tie inside one second (`.000000`); one run's events
    can tie on a microsecond. The id breaks the tie, also in the page predicate."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    ids = _timed(conn, cid, [at] * 4)

    assert _walk(conn, world["owner"], cid, limit=1) == [[i] for i in reversed(ids)]


def test_a_dropped_anchor_does_not_move_the_page_edge(conn, world):
    """The pair carries the time, so the route reads no anchor row: when the oldest event the
    client holds is gone before the next page (a dropped partial), every event of the three
    pages is still seen exactly once."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    times = [T0 + timedelta(seconds=s) for s in (3, 4, 5, 6, 7, 8, 0, 1, 2)]
    ids = _timed(conn, cid, times)

    pages = _walk(conn, world["owner"], cid, limit=3,
                  between=lambda oldest: _drop(conn, oldest["id"]))

    assert [i for page in reversed(pages) for i in page] == _by_time(ids, times)


@pytest.mark.parametrize("query", [
    {"before": "5", "before_at": "yesterday"},
    {"before": "5", "before_at": "2026-01-05T09:00:00+02:00"},     # not UTC
    {"before_at": "2026-01-05T09:00:00.000000Z"},                  # no id
])
def test_a_bad_before_at_is_400(conn, world, query):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")

    response = routes.timeline(_Req(conn, world["owner"], params={"id": cid}, query=query))

    assert response.status == 400


def test_a_bare_before_whose_event_is_gone_is_409_and_never_a_guess(conn, world):
    """The bare form reads its event's time. With that row gone the time is not inferred: the
    panel's "Older events were removed." is the honest answer, a skipped page is not."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    ids = _timed(conn, cid, [T0, T0 + timedelta(seconds=1), T0 + timedelta(seconds=2)])
    held = _page(conn, world["owner"], cid, limit="2")["events"][0]
    _drop(conn, held["id"])

    bare = routes.timeline(_Req(conn, world["owner"], params={"id": cid},
                                query={"before": str(held["id"])}))
    pair = _page(conn, world["owner"], cid, before=str(held["id"]),
                 before_at=held["created_at"])

    assert (bare.status, bare.body) == (409, {"error": "cursor_expired"})
    assert [e["id"] for e in pair["events"]] == ids[:1]


def test_a_timeline_cursor_at_or_below_the_watermark_is_409(conn, world):
    """A watermark from before migration 008 (no pair) keeps the id rule."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    ids = _events(conn, cid, 3)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO conversation_prune_watermark (conversation_id, "
                    "last_pruned_event_id) VALUES (%s, %s)", (cid, ids[0]))
    conn.commit()

    def status(before):
        return routes.timeline(_Req(conn, world["owner"], params={"id": cid},
                                    query={"before": str(before)})).status

    assert status(ids[0]) == 409
    assert status(ids[1]) == 200


@pytest.mark.parametrize(("seconds", "event_id", "expected"), [
    (2, 500, 409),                 # the watermark pair itself
    (1, 10**9, 409),               # older in time, although its id is above every pruned id
    (2, 501, 200),                 # same time, higher id
    (3, 1, 200),                   # newer in time, although its id is low
])
def test_a_pair_is_compared_with_the_watermark_pair(conn, world, seconds, event_id, expected):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO conversation_prune_watermark (conversation_id, "
                    "last_pruned_event_id, last_pruned_at, last_pruned_at_id) "
                    "VALUES (%s, 600, %s, 500)", (cid, T0 + timedelta(seconds=2)))
    conn.commit()
    before_at = service.iso(T0 + timedelta(seconds=seconds))

    response = routes.timeline(_Req(conn, world["owner"], params={"id": cid}, query={
        "before": str(event_id), "before_at": before_at}))

    assert response.status == expected


def test_the_newest_page_cursor_is_the_threads_highest_id(conn, world):
    """Not the page's newest id: an imported event older than the page has a higher id, and
    a stream started at the page's id would send it alone, out of its run."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    times = [T0 + timedelta(seconds=s) for s in (1, 2, 3, 0)]
    ids = _timed(conn, cid, times)

    body = _page(conn, world["owner"], cid, limit="2")
    later = _events(conn, cid, 1)[0]

    assert [e["id"] for e in body["events"]] == ids[1:3]
    assert body["newest_id"] == ids[3] == max(ids)
    assert later > body["newest_id"]                    # the stream sends it


def test_the_stream_still_reads_in_id_order(conn, world):
    """Replay and the stream stay on `id > cursor`: an imported event reaches a client that
    holds the newest native one."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    ids = _timed(conn, cid, [T0 + timedelta(seconds=5), T0])

    rows = service.list_events(conn, conversation_id=cid, after_id=ids[0], limit=10)

    assert [r["id"] for r in rows] == [ids[1]]


def test_an_event_time_always_has_six_fraction_digits(conn, world):
    """The panel orders events by a string compare; `isoformat()` drops a zero fraction, and
    "09:00:00Z" sorts after "09:00:00.5Z"."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    _timed(conn, cid, [T0, T0 + timedelta(microseconds=500000)])

    times = [e["created_at"] for e in _page(conn, world["owner"], cid)["events"]]

    assert times == ["2026-01-05T09:00:00.000000Z", "2026-01-05T09:00:00.500000Z"]
    assert times == sorted(times)


def test_runs_are_listed_by_start_time_newest_first(conn, world):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    native, imported = str(uuid.uuid4()), str(uuid.uuid4())
    with conn.cursor() as cur:
        for execution_id, days in ((native, 1), (imported, 30)):     # imported: higher id
            cur.execute(
                "INSERT INTO execution (execution_id, job_id, attempt, status, started_at, "
                "                       finished_at, conversation_id) "
                "VALUES (%s, 999000001, 1, 'succeeded', NOW() - INTERVAL %s DAY, "
                "        NOW() - INTERVAL %s DAY, %s)", (execution_id, days, days, cid))
    conn.commit()

    runs = service.list_runs(conn, conversation_id=cid)

    assert [r["execution_id"] for r in runs] == [native, imported]


def test_an_imported_answer_is_listed_before_a_newer_native_one(conn, world):
    """`retained()` renders messages in the order it is given, after their events are pruned."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    with conn.cursor() as cur:
        for content, at in (("native", T0 + timedelta(days=30)), ("imported", T0)):
            cur.execute("INSERT INTO message (conversation_id, role, content, created_at) "
                        "VALUES (%s, 'assistant', %s, %s)",
                        (cid, content, at))
    conn.commit()

    rows = service.list_messages(conn, conversation_id=cid, limit=10)

    assert [r["content"] for r in rows] == ["imported", "native"]


# --- the append order (P4) ---------------------------------------------------------

def test_a_second_writer_of_one_thread_waits_for_the_first_to_commit(conn, world):
    """The cursor is global: ids of one thread must commit in the order they were taken,
    or a reader at `id > cursor` moves past an id that commits later (E9 §3.4)."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    first, second = _test_connection(), _test_connection()
    done: list[int] = []
    try:
        with first.cursor() as cur:
            first_id = service.append_event(cur, cid, kind="assistant.text",
                                            source_kind="delta", source_id=970001, payload={})

        def write():
            with second.cursor() as cur:
                done.append(service.append_event(cur, cid, kind="assistant.text",
                                                 source_kind="delta", source_id=970002,
                                                 payload={}))
            second.commit()

        writer = threading.Thread(target=write)
        writer.start()
        time.sleep(0.5)
        assert done == []                    # blocked on the thread's row lock
        first.commit()
        writer.join(timeout=10)

        assert done and done[0] > first_id
    finally:
        first.close()
        second.close()


def test_only_the_service_inserts_into_the_timeline():
    """One writer (`service.append_event`) holds the append lock; a second INSERT site
    would write without it. Scans the module's string constants, not its prose."""
    src = Path(service.__file__).resolve().parent
    offenders = []
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and "INTO conversation_event" in " ".join(node.value.split())):
                offenders.append(path.name)
    assert offenders == ["service.py"]


def test_the_trigger_prompt_rides_on_run_finished_for_a_live_admin_only(conn, world):
    """`job.prompt` is written with the terminal update, after a live client already holds
    `run.started` - so `run.finished` carries it too (review impl-1 F3)."""
    job_id = _job(conn, view=world["view"], prompt="Fix the login page")
    execution_id = _claim(conn, job_id)
    ConversationFinalizer().finalize(conn=conn, job_id=job_id, attempt=1,
                                     execution_id=execution_id, outcome="failed",
                                     job_terminal=True)
    conn.commit()
    cid = _thread_of(conn, execution_id)["id"]
    rows, _, _ = service.list_timeline(conn, conversation_id=cid, before=None, limit=10)

    thread = service.load_visible(conn, conversation_id=cid, user=world["admin"])
    shown = {e["kind"]: e["payload"].get("prompt")
             for e in service.project_events(conn, rows, world["admin"], thread)}
    hidden = {e["kind"]: e["payload"].get("prompt")
              for e in service.project_events(conn, rows, world["owner"], thread)}

    assert shown == {"run.started": "Fix the login page", "run.finished": "Fix the login page"}
    assert hidden == {"run.started": None, "run.finished": None}


def test_a_role_granted_run_details_sees_them_on_that_scope_only(conn, world):
    """`conversation.run_details` is an ACL resource: admin has it, a `user` role only with
    the grant on the thread's view or workspace (owner decision D4, 2026-10-06)."""
    job_id = _job(conn, view=world["view"], prompt="Fix the login page")
    execution_id = _claim(conn, job_id)
    ConversationFinalizer().finalize(conn=conn, job_id=job_id, attempt=1,
                                     execution_id=execution_id, outcome="failed",
                                     job_terminal=True)
    conn.commit()
    thread = service.load_visible(conn, conversation_id=_thread_of(conn, execution_id)["id"],
                                  user=world["admin"])
    with conn.cursor() as cur:
        service.append_event(cur, thread["id"], kind="error", execution_id=execution_id,
                             payload={"seq": 1, "text": "raw harness error"},
                             source_kind="delta", source_id=1)
    conn.commit()
    owner = world["owner"]
    assert owner.role == "user"
    assert not service.can_see_run_details(conn, owner, thread)

    def errors(user):
        rows, _, _ = service.list_timeline(conn, conversation_id=thread["id"], before=None,
                                        limit=10)
        return [e["payload"]["text"] for e in service.project_events(conn, rows, user, thread)
                if e["kind"] == "error"]

    assert errors(owner) == [None]                 # the raw error is a run detail (U4)

    accounts.add_grant(conn, "user", "operation", service.RUN_DETAILS,
                       agent_view_id=world["other_view"])
    assert not service.can_see_run_details(conn, owner, thread)     # another view's grant
    accounts.add_grant(conn, "user", "operation", service.RUN_DETAILS,
                       workspace_id=thread["workspace_id"])
    assert service.can_see_run_details(conn, owner, thread)
    assert service.can_see_run_details(conn, world["admin"], thread)
    rows, _, _ = service.list_timeline(conn, conversation_id=thread["id"], before=None, limit=10)
    assert {e["payload"].get("prompt") for e in service.project_events(conn, rows, owner, thread)
            if e["kind"] == "run.started"} == {"Fix the login page"}
    assert errors(owner) == ["raw harness error"]


# --- review impl-1: the gaps the first round left -------------------------------

def test_regenerating_in_a_channel_thread_passes_the_same_gates_as_a_reply(conn, world):
    """F3: a regeneration is a write too - one guard, both routes (CLS-1)."""
    cid = _channel_thread(conn, world)
    message_id = _reply(conn, world["admin"], cid).body["message_id"]

    def again(user):
        return routes.regenerate(_Req(conn, user, params={"id": str(cid)},
                                      json={"message_id": message_id,
                                            "client_message_id": "again-1"}))

    assert again(world["owner"]).status == 404               # cannot even see the thread
    with conn.cursor() as cur:
        cur.execute("UPDATE conversation SET external_ref = NULL WHERE id = %s", (cid,))
    conn.commit()
    assert again(world["admin"]).status == 409               # nothing left to reply to
    with conn.cursor() as cur:
        cur.execute("UPDATE conversation SET external_ref = 'AI-7' WHERE id = %s", (cid,))
    conn.commit()
    assert again(world["admin"]).status == 201


def test_a_reply_whose_job_finished_before_it_was_attached_is_terminal(conn, world):
    """F2: `complete_pending` publishes, THEN attaches the job id. A job that finishes in
    between leaves the finalizer nothing to update - the publisher closes that turn."""
    cid = _channel_thread(conn, world)
    message_id, _, _ = service.submit_message(
        conn, conversation_id=cid, user_id=world["admin"].id,
        client_message_id="race-1", content="odpowiedź")
    with conn.cursor() as cur:            # back to the state after the first commit
        cur.execute("UPDATE message SET job_state = 'pending', job_id = NULL WHERE id = %s",
                    (message_id,))
    conn.commit()
    # The finalizer runs the moment the job exists: `publish_job` is where it lands.
    import agento.modules.conversation.src.service as service_module
    real = service_module.publish_job

    def publish_then_finish(**kwargs):
        job_id = real(**kwargs)
        with conn.cursor() as cur:
            cur.execute("UPDATE job SET status = 'SUCCESS' WHERE id = %s", (job_id,))
        conn.commit()
        return job_id

    service_module.publish_job = publish_then_finish
    try:
        service.complete_pending(conn, message_id)
    finally:
        service_module.publish_job = real

    assert _rows(conn, "SELECT job_state FROM message WHERE id = %s",
                 (message_id,))[0]["job_state"] == "terminal"


def test_a_failed_channel_reply_reaches_terminal_without_reconciliation(conn, world):
    cid = _channel_thread(conn, world)
    job_id = _reply(conn, world["admin"], cid).body["job_id"]
    execution_id = _claim(conn, job_id)

    ConversationFinalizer().finalize(conn=conn, job_id=job_id, attempt=1,
                                     execution_id=execution_id, outcome="failed",
                                     job_terminal=True)
    conn.commit()

    assert [r["job_state"] for r in _rows(
        conn, "SELECT job_state FROM message WHERE job_id = %s", (job_id,))] == ["terminal"]
    assert service.reconcile_terminal(conn) == 0        # nothing left for the backstop


def test_the_followup_workflow_executes_the_published_reply(conn, world):
    """F9/F5: the whole `execute_job` path, not just the prompt it builds."""
    from agento.framework.job_models import Job
    from agento.framework.workflows.followup import FollowupWorkflow

    cid = _channel_thread(conn, world)
    job = Job.from_row(_job_row(conn, _reply(conn, world["admin"], cid).body["job_id"]))

    class _Runner:
        def __init__(self):
            self.prompt = None

        def run(self, prompt, **kwargs):
            self.prompt = prompt
            return "ok"

    runner = _Runner()
    workflow = FollowupWorkflow(runner=runner, logger=logging.getLogger(__name__))
    workflow.execute = lambda channel, reference_id, **kw: runner.run(
        workflow.build_prompt(channel, reference_id, **kw))

    class _Context:
        config = None

    assert workflow.execute_job(_FakeChannel(), job, _Context()) == "ok"
    assert "odpowiedź" in runner.prompt


# --- recovery and the publication race, on the channel contract (review impl-2 F4) ---

def test_a_crash_after_the_publish_recovers_onto_the_same_followup_job(conn, world, seen):  # noqa: F811
    """The job is already out; only the attachment was lost. The sweep must adopt THAT job
    (its idempotency key is the message's), not publish a second external task."""
    cid = _channel_thread(conn, world)
    message_id, job_id, _ = service.submit_message(
        conn, conversation_id=cid, user_id=world["admin"].id,
        client_message_id="after-publish", content="odpowiedź")
    with conn.cursor() as cur:
        cur.execute("UPDATE message SET job_state = 'pending', job_id = NULL WHERE id = %s",
                    (message_id,))
    conn.commit()
    seen.clear()

    assert service.sweep_pending(conn, grace_seconds=0) == 1

    jobs = _rows(conn, "SELECT id, type, source FROM job "
                       "WHERE idempotency_key LIKE 'channel-reply:%%'")
    assert [(j["id"], j["type"], j["source"]) for j in jobs] == [(job_id, "followup", "jira")]
    events = [e for n, e in seen if n == "conversation_message_after"]
    assert len(events) == 1 and events[0].message_id == message_id
    assert events[0].job_id == job_id
    assert _rows(conn, "SELECT job_state FROM message WHERE id = %s",
                 (message_id,))[0]["job_state"] in ("published", "terminal")


def test_a_route_racing_the_sweep_on_a_channel_thread_announces_one_turn(conn, world, seen,  # noqa: F811
                                                                         monkeypatch):
    """EVT-4 on the channel contract: one transition, one event, one external task."""
    cid = _channel_thread(conn, world)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO message (conversation_id, role, content, "
                    "client_message_id, job_state) VALUES (%s, 'user', 'odpowiedź', "
                    "'c-race-channel', 'pending')", (cid,))
        message_id = cur.lastrowid
    conn.commit()
    seen.clear()

    done: list[int] = []
    raced: list[bool] = []
    original = service.publish_job
    other = _test_connection()

    def racing(**kwargs):
        if not raced:
            raced.append(True)
            done.append(service.complete_pending(other, message_id))
        return original(**kwargs)

    try:
        monkeypatch.setattr(service, "publish_job", racing)
        first = service.complete_pending(conn, message_id)
    finally:
        monkeypatch.setattr(service, "publish_job", original)
        other.close()

    assert first == done[0]
    assert len([e for n, e in seen if n == "conversation_message_after"]) == 1
    assert len(_rows(conn, "SELECT id FROM job "
                           "WHERE idempotency_key LIKE 'channel-reply:%%'")) == 1
