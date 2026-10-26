"""`conversation:backfill` (plan §5.7, §8.6): finished jobs get their run in their conversation."""
from __future__ import annotations

import argparse
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agento.framework.job_models import Job
from agento.framework.threads import default_ref, ensure_thread, resolve_resume
from agento.modules.conversation.src import backfill, service
from agento.modules.conversation.src.commands import ConversationBackfillCommand
from agento.modules.conversation.src.finalizer import ConversationFinalizer
from agento.modules.conversation.src.hooks import ConversationExecutionIds

FIXTURE = Path(__file__).parents[1] / "fixtures" / "claude_session_two_turns.jsonl"
SESSION = "00000000-0000-4000-8000-000000000001"
T = datetime(2026, 1, 1, 10, 0, 0)


@pytest.fixture(autouse=True)
def _runs(conn):
    yield
    with conn.cursor() as cur:
        cur.execute("DELETE FROM execution_delta")
        cur.execute("DELETE FROM execution")
    conn.commit()


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, "state_dir", lambda ws, av: tmp_path / ws / av / "state")
    return tmp_path


def _session_file(state: Path, world_codes=("c-ws", "c-av")) -> None:
    target = state.joinpath(*world_codes, "state", ".claude", "projects", "-x", f"{SESSION}.jsonl")
    target.parent.mkdir(parents=True)
    shutil.copy(FIXTURE, target)


def _job(conn, view, *, start, minutes=5, source="jira", reference="AI-1", status="SUCCESS",
         session=None, output="Done.", thread=False) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job (type, source, agent_view_id, reference_id, idempotency_key, status, "
            " attempt, max_attempts, agent_type, session_id, output, created_at, started_at, "
            " finished_at, updated_at) "
            "VALUES ('cron', %s, %s, %s, %s, %s, 1, 3, 'claude', %s, %s, %s, %s, %s, %s)",
            (source, view, reference, str(uuid.uuid4()), status, session, output, start, start,
             start + timedelta(minutes=minutes), start + timedelta(minutes=minutes)))
        job_id = cur.lastrowid
        if thread:
            cur.execute("UPDATE job SET thread_id = %s WHERE id = %s", (ensure_thread(
                cur, agent_view_id=view, ref=default_ref(source, reference, job_id)), job_id))
    conn.commit()
    return job_id


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = list(cur.fetchall())
    conn.commit()
    return rows


def _run(conn, **kw):
    return backfill.run(conn, **{"sources": None, "agent_view": None, "since": None,
                                 "limit": None, "dry_run": False, **kw})


def _timeline(conn, conversation_id):
    rows, _, _ = service.list_timeline(conn, conversation_id=conversation_id, before=None,
                                       limit=500)
    return rows


def _conversation(conn, job_id):
    return _rows(conn, "SELECT c.* FROM conversation c JOIN execution e "
                       "ON e.conversation_id = c.id WHERE e.job_id = %s", (job_id,))[0]


def test_three_jira_jobs_land_in_one_conversation_in_time_order_beside_a_native_run(
        conn, world, state):
    view = world["view"]
    native = _job(conn, view, start=T + timedelta(hours=2), status="RUNNING", thread=True)
    execution_id = ConversationExecutionIds().mint(conn=conn, job_id=native, attempt=1)
    conn.commit()
    old = [_job(conn, view, start=T + timedelta(minutes=10 * i)) for i in range(3)]

    report = _run(conn)

    assert report.fallback["jira"] == 3 and not report.errors
    cid = _conversation(conn, native)["id"]
    assert {_conversation(conn, j)["id"] for j in old} == {cid}
    starts = [e for e in _timeline(conn, cid) if e["kind"] == "run.started"]
    assert [e["execution_id"] for e in starts] == [*(f"backfill:{j}" for j in old), execution_id]
    events = _timeline(conn, cid)
    assert [e["created_at"] for e in events] == sorted(e["created_at"] for e in events)
    # Each imported run: started, its answer, finished — in this order.
    first = [e["kind"] for e in events if e["execution_id"] == f"backfill:{old[0]}"]
    assert first == ["run.started", "assistant.message", "run.finished"]


def test_a_session_file_with_two_turns_gives_two_jobs_their_events(conn, world, state):
    _session_file(state)
    a = _job(conn, world["view"], start=T, minutes=7, session=SESSION)
    b = _job(conn, world["view"], start=T + timedelta(minutes=9), session=SESSION)

    report = _run(conn)

    assert report.transcript["jira"] == 2 and not report.fallback
    events = _timeline(conn, _conversation(conn, a)["id"])
    of = {j: [e for e in events if e["execution_id"] == f"backfill:{j}"] for j in (a, b)}
    assert "tool.started" in [e["kind"] for e in of[a]]
    assert "assistant.text" in [e["kind"] for e in of[b]]
    # Each event at its line time, inside its job's run.
    for job_id, start, end in ((a, T, T + timedelta(minutes=7)),
                               (b, T + timedelta(minutes=9), T + timedelta(minutes=14))):
        times = [e["created_at"] for e in of[job_id]]
        assert times == sorted(times) and start <= times[0] and times[-1] <= end
    assert T + timedelta(minutes=3) in [e["created_at"] for e in of[a]]


def test_a_turn_count_that_does_not_match_falls_back(conn, world, state):
    _session_file(state)
    for i in range(3):
        _job(conn, world["view"], start=T + timedelta(minutes=10 * i), session=SESSION)

    report = _run(conn)

    assert report.fallback["jira"] == 3 and not report.transcript
    assert not _rows(conn, "SELECT 1 FROM conversation_event WHERE kind = 'assistant.text'")


def test_a_second_run_and_a_dry_run_write_nothing(conn, world, state):
    _job(conn, world["view"], start=T)

    dry = _run(conn, dry_run=True)
    assert dry.fallback["jira"] == 1
    assert not _rows(conn, "SELECT 1 FROM execution")
    assert _rows(conn, "SELECT thread_id FROM job")[0]["thread_id"] is None

    _run(conn)
    count = len(_rows(conn, "SELECT id FROM conversation_event"))
    again = _run(conn)
    assert not again.fallback and again.skipped["jira"]["has_execution"] == 1
    assert len(_rows(conn, "SELECT id FROM conversation_event")) == count


def test_jobs_outside_the_window_or_with_no_view_are_skipped(conn, world, state):
    old = _job(conn, world["view"], start=T)
    gone = _job(conn, None, start=T + timedelta(days=30))
    kept = _job(conn, world["view"], start=T + timedelta(days=30), reference=None)

    report = _run(conn, since=T + timedelta(days=1))

    assert report.fallback["jira"] == 1
    assert report.skipped["jira"] == {"has_execution": 0, "no_view": 1, "outside_window": 1}
    assert {r["job_id"] for r in _rows(conn, "SELECT job_id FROM execution")} == {kept}
    assert old not in {r["job_id"] for r in _rows(conn, "SELECT job_id FROM execution")}
    assert gone != kept
    # No reference: the job's own thread.
    assert _rows(conn, "SELECT t.ref FROM job j JOIN thread t ON t.id = j.thread_id "
                       "WHERE j.id = %s", (kept,))[0]["ref"] == f"job:{kept}"


@pytest.mark.parametrize("failure", ["finder", "file"])
def test_a_session_that_cannot_be_read_falls_back_and_the_next_job_imports(
        conn, world, state, monkeypatch, failure):
    if failure == "file":
        target = state / "c-ws" / "c-av" / "state" / ".claude" / "projects" / "-x" / f"{SESSION}.jsonl"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"\xff\xfe not text\n")
    else:
        adapter = backfill._reader("claude")
        monkeypatch.setattr(type(adapter), "session_file",
                            lambda self, root, sid: (_ for _ in ()).throw(RuntimeError("x")))
    _job(conn, world["view"], start=T, session=SESSION)
    _job(conn, world["view"], start=T + timedelta(minutes=10), session=SESSION)
    _job(conn, world["view"], start=T + timedelta(minutes=20), reference="AI-9")

    report = _run(conn)

    assert report.fallback["jira"] == 3 and not report.errors


def test_the_selection_is_read_in_pages_and_the_limit_holds_across_them(
        conn, world, state, monkeypatch):
    monkeypatch.setattr(backfill, "PAGE", 2)
    for i in range(5):
        _job(conn, world["view"], start=T + timedelta(minutes=i), reference=f"AI-{i}")

    assert _run(conn, limit=3).fallback["jira"] == 3
    assert _run(conn).fallback["jira"] == 2


def test_two_answers_in_one_second_keep_their_time_order(conn, world, state):
    """Answers keep microseconds: the older one imported after the newer is still first."""
    view = world["view"]
    newer = _job(conn, view, start=T, reference="AI-5", output="newer")
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET finished_at = %s WHERE id = %s",
                    (T + timedelta(milliseconds=2), newer))
    conn.commit()
    _run(conn)
    older = _job(conn, view, start=T, reference="AI-5", output="older")
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET started_at = %s, finished_at = %s WHERE id = %s",
                    (T - timedelta(seconds=1), T, older))
    conn.commit()
    _run(conn)

    rows = service.list_messages(conn, conversation_id=_conversation(conn, newer)["id"], limit=10)
    assert [r["content"] for r in rows] == ["older", "newer"]


def test_an_excluded_source_is_never_imported(conn, world, state):
    _job(conn, world["view"], start=T, source="subagent")
    assert not _run(conn).fallback


def test_a_new_job_on_the_issue_resumes_the_last_imported_job(conn, world, state):
    view = world["view"]
    _job(conn, view, start=T, session="s-1")
    last = _job(conn, view, start=T + timedelta(minutes=10), session="s-2")
    _run(conn)

    new = _job(conn, view, start=T + timedelta(hours=1), status="TODO", thread=True)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM job WHERE id = %s", (new,))
        assert resolve_resume(cur, Job.from_row(cur.fetchone())) == last
    conn.commit()


def test_last_activity_is_the_newest_imported_time_and_never_moves_back(conn, world, state):
    _session_file(state)
    a = _job(conn, world["view"], start=T, minutes=7, session=SESSION, reference="AI-2")
    _job(conn, world["view"], start=T + timedelta(minutes=9), session=SESSION, reference="AI-2")
    _run(conn)
    conversation = _conversation(conn, a)
    newest = _rows(conn, "SELECT MAX(created_at) AS at FROM conversation_event "
                         "WHERE conversation_id = %s", (conversation["id"],))[0]["at"]
    assert conversation["last_activity_at"] == newest.replace(microsecond=0)

    # A thread with a newer native run keeps that run's time.
    native = _job(conn, world["view"], start=datetime(2026, 6, 1), status="RUNNING",
                  reference="AI-3", thread=True)
    execution_id = ConversationExecutionIds().mint(conn=conn, job_id=native, attempt=1)
    ConversationFinalizer().finalize(conn=conn, job_id=native, attempt=1,
                                     execution_id=execution_id, outcome="succeeded",
                                     job_terminal=True)
    conn.commit()
    before = _conversation(conn, native)["last_activity_at"]
    _job(conn, world["view"], start=T, reference="AI-3")
    _run(conn)
    assert _conversation(conn, native)["last_activity_at"] == before


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_limit_below_one_is_a_usage_error(value):
    parser = argparse.ArgumentParser()
    ConversationBackfillCommand().configure(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["--limit", value])


def test_a_raw_stream_output_imports_only_its_answer(conn, world, state):
    """An older Claude job kept its whole stream-json in `job.output`."""
    stream = ('{"type":"system","subtype":"init","session_id":"s"}\n'
              '{"type":"result","subtype":"success","result":"Final answer.","session_id":"s"}\n')
    job = _job(conn, world["view"], start=T, output=stream)

    _run(conn)

    rows = service.list_messages(conn, conversation_id=_conversation(conn, job)["id"], limit=10)
    assert [r["content"] for r in rows] == ["Final answer."]
