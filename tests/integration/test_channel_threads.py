"""Every job in a thread, and who may read it (E9 §3.4-§3.7).

A panel job runs in the thread of the message it answers. Any other job (Jira, Outlook, a
cron) runs in its **channel thread**: one per `(source, view, reference)`, with no owner,
read-only, and readable by an admin only.
"""
from __future__ import annotations

import ast
import json
import threading
import time
import uuid
from pathlib import Path

import pytest

from agento.framework.execution_hooks import DeltaRecord
from agento.modules.conversation.src import retention, routes, service, stream
from agento.modules.conversation.src.deltas import ConversationDeltaSink
from agento.modules.conversation.src.finalizer import ConversationFinalizer
from agento.modules.conversation.src.hooks import ConversationExecutionIds

from .conftest import _test_connection
from .test_conversation_submission import _job_type, _Req  # noqa: F401


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


def test_an_admin_lists_and_reads_a_channel_thread_but_cannot_post_into_it(conn, world):
    cid = _channel_thread(conn, world)
    admin = world["admin"]

    listed = routes.index(_Req(conn, admin, query={"scope": "channels", "channel": "jira"}))
    assert [t["id"] for t in listed.body] == [cid]
    assert listed.body[0]["live"] is True                 # its run is still running
    shown = routes.show(_Req(conn, admin, params={"id": cid}))
    assert shown.status == 200 and len(shown.body["runs"]) == 1
    page = routes.timeline(_Req(conn, admin, params={"id": cid}))
    assert [e["kind"] for e in page.body["events"]] == ["run.started"]

    sent = routes.send(_Req(conn, admin, params={"id": cid},
                            json={"client_message_id": "x", "content": "hi"}))
    assert (sent.status, sent.body) == (409, {"error": "read_only"})


def test_the_channel_list_pages_by_its_cursor_without_overlap(conn, world, monkeypatch):
    ids = {_thread_of(conn, _claim(conn, _job(conn, reference=f"AI-{n}")))["id"]
           for n in range(5)}
    real = service.config
    monkeypatch.setattr(service, "config", lambda c, path: 2 if path == "history/page_size"
                        else real(c, path))
    seen, before = [], None
    for _ in range(4):
        query = {"scope": "channels", **({"before": before} if before else {})}
        page = routes.index(_Req(conn, world["admin"], query=query)).body
        if not page:
            break
        seen += [t["id"] for t in page]
        before = page[-1]["cursor"]

    assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen))


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

def _events(conn, cid: int, n: int) -> list[int]:
    out = []
    with conn.cursor() as cur:
        for i in range(n):
            out.append(service.append_event(cur, cid, kind="assistant.text",
                                             source_kind="delta", source_id=980000 + i,
                                             payload={"seq": i, "text": str(i)}))
    conn.commit()
    return out


def test_the_timeline_pages_back_with_no_gap_and_no_overlap(conn, world):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    ids = _events(conn, cid, 7)

    pages, before = [], None
    while True:
        query = {"limit": "3", **({"before": str(before)} if before else {})}
        body = routes.timeline(_Req(conn, world["owner"], params={"id": cid},
                                    query=query)).body
        pages.append([e["id"] for e in body["events"]])
        if not body["has_older"]:
            break
        before = pages[-1][0]

    assert pages == [ids[4:], ids[1:4], ids[:1]]          # each page oldest first
    assert pages[0][-1] == ids[-1]


def test_a_timeline_cursor_at_or_below_the_watermark_is_409(conn, world):
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
    rows, _ = service.list_timeline(conn, conversation_id=cid, before_id=None, limit=10)

    shown = {e["kind"]: e["payload"].get("prompt")
             for e in service.project_events(conn, rows, world["admin"])}
    hidden = {e["kind"]: e["payload"].get("prompt")
              for e in service.project_events(conn, rows, world["owner"])}

    assert shown == {"run.started": "Fix the login page", "run.finished": "Fix the login page"}
    assert hidden == {"run.started": None, "run.finished": None}
