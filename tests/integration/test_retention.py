"""Retention, auto-archive and `cursor_expired` (PRD E3-E5 §10.1).

The prune moves a watermark; the watermark is what makes a stale cursor answerable. Both
cursor paths ask the same helper, because checking only the route would hand a reconnecting
browser the surviving rows with the removed ones silently missing.
"""
from __future__ import annotations

import random
import uuid

import pytest

from agento.framework.event_manager import ObserverEntry, clear, get_event_manager
from agento.modules.conversation.src import retention, routes, service, stream

from .conftest import _clean  # noqa: F401
from .test_conversation_submission import _job_type, _Req  # noqa: F401


class _QReq(_Req):
    def __init__(self, conn, user, params=None, query=None):
        super().__init__(conn, user, params=params)
        self.query = query or {}


@pytest.fixture
def thread(conn, world):
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    yield conversation_id
    _wipe(conn, conversation_id)


def _wipe(conn, conversation_id):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM conversation_event WHERE conversation_id = %s",
                    (conversation_id,))
        cur.execute("DELETE FROM conversation_prune_watermark WHERE conversation_id = %s",
                    (conversation_id,))
    conn.commit()


def _event(conn, conversation_id, *, age_days: float = 0, kind="assistant.message") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversation_event "
            "(conversation_id, execution_id, kind, payload, source_kind, source_id, "
            " created_at) "
            "VALUES (%s, NULL, %s, '{}', 'outbox', %s, NOW() - INTERVAL %s SECOND)",
            (conversation_id, kind, random.getrandbits(48), int(age_days * 86400)))
        row_id = cur.lastrowid
    conn.commit()
    return row_id


def _rows(conn, sql, args=()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = list(cur.fetchall())
    conn.commit()
    return rows


def _event_ids(conn, conversation_id) -> list[int]:
    return [r["id"] for r in _rows(
        conn, "SELECT id FROM conversation_event WHERE conversation_id = %s ORDER BY id",
        (conversation_id,))]


def _watermark(conn, conversation_id) -> int | None:
    rows = _rows(conn, "SELECT last_pruned_event_id AS w FROM conversation_prune_watermark "
                       "WHERE conversation_id = %s", (conversation_id,))
    return int(rows[0]["w"]) if rows else None


# --- the age prune ---------------------------------------------------------

def test_events_older_than_the_window_go_and_newer_ones_stay(conn, thread):
    old = _event(conn, thread, age_days=100)
    recent = _event(conn, thread, age_days=1)

    retention.prune_events(conn, event_days=90)

    assert _event_ids(conn, thread) == [recent]
    assert old not in _event_ids(conn, thread)


def test_a_row_exactly_on_the_cutoff_is_kept(conn, thread):
    """Strict `<`, plan-wide. A boundary row is inside the window, not outside it."""
    boundary = _event(conn, thread, age_days=90)

    retention.prune_events(conn, event_days=90)

    assert boundary in _event_ids(conn, thread)


def test_the_watermark_is_the_highest_removed_id_not_the_highest_surviving(conn, thread):
    first = _event(conn, thread, age_days=100)
    second = _event(conn, thread, age_days=95)
    survivor = _event(conn, thread, age_days=1)

    retention.prune_events(conn, event_days=90)

    assert _watermark(conn, thread) == second
    assert second > first and survivor > second


def test_a_second_pass_is_a_no_op_and_does_not_lower_the_watermark(conn, thread):
    _event(conn, thread, age_days=100)
    removed = _event(conn, thread, age_days=95)
    _event(conn, thread, age_days=1)
    retention.prune_events(conn, event_days=90)

    assert retention.prune_events(conn, event_days=90) == 0
    assert _watermark(conn, thread) == removed


def test_a_conversation_entirely_inside_the_window_gets_no_watermark_row(conn, thread):
    _event(conn, thread, age_days=1)

    retention.prune_events(conn, event_days=90)

    assert _watermark(conn, thread) is None


def test_the_prune_runs_in_a_live_conversation_not_only_a_deleted_one(conn, thread):
    _event(conn, thread, age_days=100)

    retention.prune_events(conn, event_days=90)

    status = _rows(conn, "SELECT status FROM conversation WHERE id = %s", (thread,))
    assert status[0]["status"] == "active"


def test_the_window_has_a_floor_of_one_day(conn, thread):
    """A zero or negative window would delete rows the stream has not sent yet."""
    fresh = _event(conn, thread, age_days=0)

    retention.prune_events(conn, event_days=0)

    assert fresh in _event_ids(conn, thread)


def test_two_conversations_keep_their_own_watermarks(conn, world, thread):
    other = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="other")
    try:
        mine = _event(conn, thread, age_days=100)
        theirs = _event(conn, other, age_days=100)

        retention.prune_events(conn, event_days=90)

        assert _watermark(conn, thread) == mine
        assert _watermark(conn, other) == theirs
    finally:
        _wipe(conn, other)


# --- cursor_expired --------------------------------------------------------

def test_a_cursor_at_the_watermark_is_expired(conn, thread):
    removed = _event(conn, thread, age_days=100)
    _event(conn, thread, age_days=1)
    retention.prune_events(conn, event_days=90)

    assert retention.cursor_expired(conn, conversation_id=thread, cursor=removed) is True


def test_a_cursor_above_the_watermark_is_fine(conn, thread):
    _event(conn, thread, age_days=100)
    survivor = _event(conn, thread, age_days=1)
    retention.prune_events(conn, event_days=90)

    assert retention.cursor_expired(conn, conversation_id=thread,
                                    cursor=survivor) is False


def test_a_conversation_with_no_watermark_has_no_expired_cursor(conn, thread):
    assert retention.cursor_expired(conn, conversation_id=thread, cursor=1) is False


def test_no_cursor_is_never_expired(conn, thread):
    _event(conn, thread, age_days=100)
    retention.prune_events(conn, event_days=90)

    assert retention.cursor_expired(conn, conversation_id=thread, cursor=None) is False


def test_an_empty_conversation_has_no_expired_cursor(conn, thread):
    retention.prune_events(conn, event_days=90)

    assert retention.cursor_expired(conn, conversation_id=thread, cursor=999) is False


def test_interleaved_ids_across_two_conversations_do_not_cross_watermarks(conn, world,
                                                                          thread):
    """`conversation_event.id` is global, so one thread's watermark sits in the middle of
    another's ids. The check is per conversation or it expires the wrong cursors."""
    other = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="other")
    try:
        old_mine = _event(conn, thread, age_days=100)
        new_theirs = _event(conn, other, age_days=1)
        retention.prune_events(conn, event_days=90)

        assert retention.cursor_expired(conn, conversation_id=thread,
                                        cursor=old_mine) is True
        assert retention.cursor_expired(conn, conversation_id=other,
                                        cursor=new_theirs) is False
        assert retention.cursor_expired(conn, conversation_id=other,
                                        cursor=old_mine) is False
    finally:
        _wipe(conn, other)


# --- both cursor paths ask the same question -------------------------------

def test_the_replay_route_answers_409_cursor_expired(conn, world, thread):
    removed = _event(conn, thread, age_days=100)
    _event(conn, thread, age_days=1)
    retention.prune_events(conn, event_days=90)

    response = routes.events(_QReq(conn, world["owner"], params={"id": str(thread)},
                                   query={"after": str(removed)}))

    assert (response.status, response.body) == (409, {"error": "cursor_expired"})


def test_the_replay_route_still_serves_a_live_cursor(conn, world, thread):
    _event(conn, thread, age_days=100)
    first = _event(conn, thread, age_days=1)
    second = _event(conn, thread, age_days=1)
    retention.prune_events(conn, event_days=90)

    response = routes.events(_QReq(conn, world["owner"], params={"id": str(thread)},
                                   query={"after": str(first)}))

    assert response.status == 200
    assert [e["id"] for e in response.body] == [second]


def test_a_stream_opened_with_an_expired_cursor_emits_cursor_expired_and_no_event(
        conn, world, thread, monkeypatch):
    """The worse of the two failures: a browser reconnecting after a prune must be TOLD."""
    import datetime

    from agento.framework.access import sessions

    monkeypatch.setattr(sessions, "lookup_session", lambda c, t: sessions.Session(
        id="sid", user=world["owner"],
        expires_at=datetime.datetime.now() + datetime.timedelta(hours=1)))
    removed = _event(conn, thread, age_days=100)
    _event(conn, thread, age_days=1)
    retention.prune_events(conn, event_days=90)

    chunks = list(stream.frames(conn, conversation_id=thread, session_token="tok",
                                cursor=removed, user_id=world["owner"].id,
                                now=lambda: 0.0, sleep=lambda s: None))

    assert chunks == [b"event: cursor_expired\ndata: {}\n\n"]


def test_a_stream_above_the_watermark_streams_normally(conn, world, thread, monkeypatch):
    import datetime

    from agento.framework.access import sessions

    monkeypatch.setattr(sessions, "lookup_session", lambda c, t: sessions.Session(
        id="sid", user=world["owner"],
        expires_at=datetime.datetime.now() + datetime.timedelta(hours=1)))
    _event(conn, thread, age_days=100)
    first = _event(conn, thread, age_days=1)
    second = _event(conn, thread, age_days=1)
    retention.prune_events(conn, event_days=90)

    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += 1000

    gen = stream.frames(conn, conversation_id=thread, session_token="tok",
                        cursor=first, user_id=world["owner"].id,
                        now=lambda: clock["t"], sleep=sleep)
    chunks = list(gen)

    assert any(f"id: {second}\n".encode() in c for c in chunks)


def test_an_expired_stream_takes_no_slot(conn, world, thread, monkeypatch):
    """It returns before `acquire_slot`, so a prune cannot evict live streams."""
    import datetime

    from agento.framework.access import sessions

    monkeypatch.setattr(sessions, "lookup_session", lambda c, t: sessions.Session(
        id="sid", user=world["owner"],
        expires_at=datetime.datetime.now() + datetime.timedelta(hours=1)))
    removed = _event(conn, thread, age_days=100)
    _event(conn, thread, age_days=1)
    retention.prune_events(conn, event_days=90)
    stream._open_streams.clear()

    list(stream.frames(conn, conversation_id=thread, session_token="tok", cursor=removed,
                       user_id=world["owner"].id, now=lambda: 0.0, sleep=lambda s: None))

    assert stream.live_streams(world["owner"].id) == 0


# --- auto-archive ----------------------------------------------------------

def _message(conn, conversation_id, *, role="user", job_state="terminal", job_id=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO message (conversation_id, role, content, client_message_id, "
            " job_state, job_id) VALUES (%s, %s, 'x', %s, %s, %s)",
            (conversation_id, role, str(uuid.uuid4()), job_state, job_id))
        message_id = cur.lastrowid
    conn.commit()
    return message_id


def _age_conversation(conn, conversation_id, days):
    """Age the thread's activity: every message in it, and its own `created_at` for the
    case where it has none. `updated_at` is deliberately NOT aged - idle is the newest
    message's clock, and the tests below pin that the row's write time does not vote."""
    with conn.cursor() as cur:
        cur.execute("UPDATE message SET created_at = NOW() - INTERVAL %s DAY "
                    "WHERE conversation_id = %s", (days, conversation_id))
        cur.execute("UPDATE conversation SET created_at = NOW() - INTERVAL %s DAY "
                    "WHERE id = %s", (days, conversation_id))
    conn.commit()


def _age_archive(conn, conversation_id, days):
    """Age the ARCHIVE clock - `conversation.updated_at`, which is when the row was last
    written and therefore when it was archived. The delete window counts from that, not
    from the last message, so it is aged on its own."""
    with conn.cursor() as cur:
        cur.execute("UPDATE conversation SET updated_at = NOW() - INTERVAL %s DAY "
                    "WHERE id = %s", (days, conversation_id))
    conn.commit()


@pytest.fixture
def seen():
    """Every archive event the pass dispatches."""
    clear()
    captured: list[tuple[str, object]] = []
    for name in ("conversation_archive_after", "conversation_reactivate_after"):
        get_event_manager().register(name, ObserverEntry(
            name=f"spy_{name}",
            observer_class=type("Spy", (), {
                "execute": (lambda n: lambda self, event: captured.append((n, event)))(name),
            })))
    yield captured
    clear()


def _status(conn, conversation_id) -> str | None:
    rows = _rows(conn, "SELECT status FROM conversation WHERE id = %s", (conversation_id,))
    return rows[0]["status"] if rows else None


def test_an_idle_thread_is_archived(conn, thread):
    _message(conn, thread, job_state="terminal")
    _age_conversation(conn, thread, 100)

    assert retention.auto_archive(conn, idle_days=30) == 1
    assert _status(conn, thread) == "archived"


def test_a_recently_touched_thread_is_left_alone(conn, thread):
    _message(conn, thread, job_state="terminal")

    assert retention.auto_archive(conn, idle_days=30) == 0
    assert _status(conn, thread) == "active"


@pytest.mark.parametrize("job_state", ["published", "pending"])
def test_a_thread_whose_newest_user_turn_is_not_terminal_is_skipped(conn, thread,
                                                                    job_state):
    """It is waiting for an answer, not idle. Archiving it would hide a thread the user is
    still owed a reply on. `published` covers a running turn AND a paused one - the message
    row carries the same state for both, and the predicate is `<> terminal`, not a list of
    the states it happens to know today."""
    _message(conn, thread, job_state="terminal")
    _message(conn, thread, job_state=job_state)
    _age_conversation(conn, thread, 100)

    assert retention.auto_archive(conn, idle_days=30) == 0
    assert _status(conn, thread) == "active"


def test_a_non_terminal_assistant_row_does_not_block_the_archive(conn, thread):
    """`job_state` belongs to the user turn; the check must name the role or a thread with
    an assistant row could never retire."""
    _message(conn, thread, role="user", job_state="terminal")
    _message(conn, thread, role="assistant", job_state=None)
    _age_conversation(conn, thread, 100)

    assert retention.auto_archive(conn, idle_days=30) == 1


def test_a_rename_or_a_reactivation_does_not_postpone_the_archive(conn, thread):
    """`conversation.updated_at` does not vote.

    It moves whenever the row is written - a rename, an archive, a reactivation - so
    requiring it to be old TOO means one rename keeps a dead thread out of retention for
    ever. §10.1 names one clock: "a conversation whose last message is older than
    idle_days".
    """
    _message(conn, thread, job_state="terminal")
    _age_conversation(conn, thread, 100)
    with conn.cursor() as cur:                      # a rename, just now
        cur.execute("UPDATE conversation SET title = 'renamed' WHERE id = %s", (thread,))
    conn.commit()

    assert retention.auto_archive(conn, idle_days=30) == 1
    assert _status(conn, thread) == "archived"


def test_a_thread_with_no_messages_falls_back_to_its_own_created_at(conn, thread):
    """It has no message clock; its birth is the only activity it has ever had."""
    assert retention.auto_archive(conn, idle_days=30) == 0       # born just now

    _age_conversation(conn, thread, 100)

    assert retention.auto_archive(conn, idle_days=30) == 1


def test_a_thread_answered_a_minute_ago_is_not_idle_however_old_its_row_is(conn, thread):
    """`conversation.updated_at` is NOT the thread's last activity.

    Posting a message never writes the conversation row, so on `updated_at` alone every
    long-lived thread eventually reads as idle and is archived out of the user's list
    while it is still being used - §10.1 says "a conversation whose last message is older
    than idle_days", and that is the message's clock.
    """
    _message(conn, thread, job_state="terminal")
    _age_conversation(conn, thread, 100)
    _message(conn, thread, job_state="terminal")        # answered just now

    assert retention.auto_archive(conn, idle_days=30) == 0
    assert _status(conn, thread) == "active"


def test_a_post_between_the_select_and_the_archive_keeps_the_thread(conn, thread):
    """The bulk select and the archive are not one statement.

    `still_idle` is the re-check that closes the window, and it runs under the same
    conversation row lock `service.reactivate` takes on the posting path, so the two
    serialize instead of the archive retiring a thread that is live again.
    """
    _message(conn, thread, job_state="terminal")
    _age_conversation(conn, thread, 100)
    assert retention.still_idle(conn, thread, idle_days=30) is True
    conn.commit()

    _message(conn, thread, job_state="terminal")        # the post lands in the window

    assert retention.still_idle(conn, thread, idle_days=30) is False
    conn.commit()


def test_auto_archive_dispatches_the_idle_reason_with_no_actor(conn, thread, seen):
    _message(conn, thread, job_state="terminal")
    _age_conversation(conn, thread, 100)

    retention.auto_archive(conn, idle_days=30)

    names = [n for n, _ in seen]
    assert names == ["conversation_archive_after"]
    event = seen[0][1]
    assert (event.reason, event.actor_id) == ("idle", None)


def test_an_already_archived_thread_is_not_archived_twice(conn, thread, seen):
    _message(conn, thread, job_state="terminal")
    _age_conversation(conn, thread, 100)
    retention.auto_archive(conn, idle_days=30)
    seen.clear()

    assert retention.auto_archive(conn, idle_days=30) == 0
    assert seen == []


# --- the ordered delete ----------------------------------------------------

def _execution(conn, *, job_id: int) -> str:
    execution_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO execution (execution_id, job_id, attempt, status, conversation_id) "
            "VALUES (%s, %s, 1, 'succeeded', "
            "        (SELECT conversation_id FROM message WHERE job_id = %s LIMIT 1))",
            (execution_id, job_id, job_id))
        cur.execute(
            "INSERT INTO execution_delta (execution_id, seq, kind) VALUES (%s, 1, 'delta')",
            (execution_id,))
    conn.commit()
    return execution_id


def _tree(conn, conversation_id) -> dict[str, int]:
    """Everything that hangs off one conversation, counted where it actually lives."""
    def count(sql, args):
        return int(_rows(conn, sql, args)[0]["n"])
    return {
        "events": count("SELECT COUNT(*) AS n FROM conversation_event "
                        "WHERE conversation_id = %s", (conversation_id,)),
        "messages": count("SELECT COUNT(*) AS n FROM message WHERE conversation_id = %s",
                          (conversation_id,)),
        "executions": count(
            "SELECT COUNT(*) AS n FROM execution e JOIN message m ON m.job_id = e.job_id "
            "WHERE m.conversation_id = %s", (conversation_id,)),
        "deltas": count(
            "SELECT COUNT(*) AS n FROM execution_delta d "
            "JOIN execution e ON e.execution_id = d.execution_id "
            "JOIN message m ON m.job_id = e.job_id WHERE m.conversation_id = %s",
            (conversation_id,)),
        "watermarks": count("SELECT COUNT(*) AS n FROM conversation_prune_watermark "
                            "WHERE conversation_id = %s", (conversation_id,)),
    }


@pytest.fixture
def loaded(conn, thread):
    """A conversation with a row in every table the delete has to reach."""
    job_id = random.getrandbits(31)
    _message(conn, thread, job_id=job_id)
    execution_id = _execution(conn, job_id=job_id)
    _event(conn, thread, age_days=100)
    _event(conn, thread, age_days=1)
    retention.prune_events(conn, event_days=90)
    assert _tree(conn, thread) == {"events": 1, "messages": 1, "executions": 1,
                                   "deltas": 1, "watermarks": 1}
    return thread, execution_id


def test_the_delete_removes_every_table_in_the_tree(conn, loaded):
    conversation_id, _ = loaded

    assert retention.delete_tree(conn, conversation_id) is True

    assert _tree(conn, conversation_id) == {"events": 0, "messages": 0, "executions": 0,
                                            "deltas": 0, "watermarks": 0}
    assert _status(conn, conversation_id) is None


def test_the_execution_rows_go_although_no_cascade_reaches_them(conn, loaded):
    """`execution` and `execution_delta` hang off `job`, not off `conversation`: dropping
    the conversation row alone would orphan them for ever."""
    conversation_id, execution_id = loaded

    retention.delete_tree(conn, conversation_id)

    assert _rows(conn, "SELECT 1 AS x FROM execution WHERE execution_id = %s",
                 (execution_id,)) == []
    assert _rows(conn, "SELECT 1 AS x FROM execution_delta WHERE execution_id = %s",
                 (execution_id,)) == []


def test_a_missing_conversation_is_not_an_error(conn):
    assert retention.delete_tree(conn, 2**40) is False


def test_the_retention_pass_deletes_only_what_is_archived_and_past_the_window(conn, world,
                                                                             thread):
    keep_active = thread
    old = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="old")
    fresh = service.create_conversation(conn, user_id=world["owner"].id,
                                        agent_view_id=world["view"], title="fresh")
    try:
        service.archive(conn, old)
        service.archive(conn, fresh)
        _age_archive(conn, old, 100)

        assert retention.delete_archived(conn, archived_days=30) == 1
        assert _status(conn, old) is None
        assert _status(conn, fresh) == "archived"
        assert _status(conn, keep_active) == "active"
    finally:
        _wipe(conn, fresh)


def test_a_reactivation_during_the_pass_wins_and_the_thread_survives(conn, thread):
    """The re-check under the lock is what makes this a decision instead of a race: the
    thread is either deleted whole or kept whole, never half of each."""
    service.archive(conn, thread)
    _age_archive(conn, thread, 100)
    _event(conn, thread, age_days=1)

    service.reactivate(conn, thread, actor_id=1)
    assert retention.delete_archived(conn, archived_days=30) == 0

    assert _status(conn, thread) == "active"
    assert _tree(conn, thread)["events"] == 1


def test_a_second_run_finishes_a_conversation_left_archived(conn, thread):
    """A crash mid-delete leaves an archived conversation, never an orphan: the next run
    re-runs the same ordered delete and completes it."""
    service.archive(conn, thread)
    _age_archive(conn, thread, 100)
    _event(conn, thread, age_days=1)
    with conn.cursor() as cur:  # the first pass got as far as the events and died
        cur.execute("DELETE FROM conversation_event WHERE conversation_id = %s", (thread,))
    conn.commit()

    assert retention.delete_archived(conn, archived_days=30) == 1
    assert _status(conn, thread) is None


def test_the_operator_delete_runs_the_same_ordered_delete(conn, loaded):
    """`delete_conversation` must not fall back to the cascade, or the operator path would
    leave behind exactly the rows the retention path removes."""
    conversation_id, execution_id = loaded

    service.delete_conversation(conn, conversation_id)

    assert _rows(conn, "SELECT 1 AS x FROM execution WHERE execution_id = %s",
                 (execution_id,)) == []


def _run(conn, *, status="succeeded", started_days=0, finished_days=0,
         conversation_id=None) -> str:
    execution_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO execution (execution_id, job_id, attempt, status, started_at, "
            "                       finished_at, conversation_id) "
            "VALUES (%s, 999000001, 1, %s, NOW() - INTERVAL %s DAY, "
            "        IF(%s = 'running', NULL, NOW() - INTERVAL %s DAY), %s)",
            (execution_id, status, started_days, status, finished_days, conversation_id))
        cur.execute("INSERT INTO execution_delta (execution_id, seq, kind) "
                    "VALUES (%s, 1, 'assistant.text')", (execution_id,))
    conn.commit()
    return execution_id


def _alive(conn, *ids) -> set[str]:
    marks = ", ".join(["%s"] * len(ids))
    runs = {r["execution_id"] for r in _rows(
        conn, f"SELECT execution_id FROM execution WHERE execution_id IN ({marks})", ids)}
    deltas = {r["execution_id"] for r in _rows(
        conn, f"SELECT execution_id FROM execution_delta WHERE execution_id IN ({marks})", ids)}
    assert runs == deltas            # a run and its ledger go together
    return runs


def _drop(conn, *ids) -> None:
    marks = ", ".join(["%s"] * len(ids))
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM execution_delta WHERE execution_id IN ({marks})", ids)
        cur.execute(f"DELETE FROM execution WHERE execution_id IN ({marks})", ids)
        cur.execute(f"DELETE FROM tool_invocation WHERE run_execution_id IN ({marks})", ids)
    conn.commit()


def test_an_old_finished_run_of_an_active_thread_is_pruned_with_its_ledger(conn, thread):
    """One age bound for every run (E9 §3.5): an active channel thread lives as long as its
    issue keeps running, and its runs must not grow with it for ever (CODE-8)."""
    old = _run(conn, started_days=30, finished_days=30, conversation_id=thread)
    young = _run(conn, started_days=1, finished_days=1, conversation_id=thread)
    try:
        assert retention.prune_old_executions(conn, event_days=7) == 1
        assert _alive(conn, old, young) == {young}
    finally:
        _drop(conn, old, young)


def test_a_running_run_is_never_pruned(conn):
    running = _run(conn, status="running", started_days=30)
    try:
        assert retention.prune_old_executions(conn, event_days=7) == 0
        assert _alive(conn, running) == {running}
    finally:
        _drop(conn, running)


def test_the_clock_is_when_the_run_finished_not_when_it_started(conn):
    """Events age by `created_at`; a run that started long ago and finished today still has
    young events, so its row must stay as long as they do."""
    late = _run(conn, started_days=30, finished_days=0)
    try:
        assert retention.prune_old_executions(conn, event_days=7) == 0
        assert _alive(conn, late) == {late}
    finally:
        _drop(conn, late)


def test_a_run_with_an_unprojected_tool_call_waits_for_the_relay(conn):
    """The relay finds a call's thread through the run's row, so the row stays until every
    finished call is projected - then it goes."""
    old = _run(conn, started_days=30, finished_days=30)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tool_invocation (execution_id, run_execution_id, capability_id, "
            "transport, actor, subject_id, tool_name, args_sha256, workspace_id, outcome) "
            "VALUES (%s, %s, 1, 'http', 'agent', '7', 'x', %s, 3, 'ok')",
            (str(uuid.uuid4()), old, "a" * 64))
    conn.commit()
    try:
        assert retention.prune_old_executions(conn, event_days=7) == 0
        with conn.cursor() as cur:
            cur.execute("UPDATE tool_invocation SET conversation_relayed_at = NOW() "
                        "WHERE run_execution_id = %s", (old,))
        conn.commit()
        assert retention.prune_old_executions(conn, event_days=7) == 1
        assert _alive(conn, old) == set()
    finally:
        _drop(conn, old)


def test_every_pass_stops_at_its_per_run_cap(conn, world):
    """CODE-8. Each pass loads its ids before it works on them, so an unbounded SELECT is an
    unbounded list in memory and one nightly run that never ends. What a run leaves, the next
    run starts with - oldest first, so nothing is skipped for ever.

    One test for the class: all four passes, each given more eligible rows than its cap.
    """
    threads = []
    for _ in range(3):
        cid = service.create_conversation(conn, user_id=world["owner"].id,
                                          agent_view_id=world["view"], title="t")
        threads.append(cid)
        _event(conn, cid, age_days=400)
        _age_conversation(conn, cid, 400)
    orphans = [_run(conn, started_days=40, finished_days=40) for _ in range(3)]

    try:
        assert retention.prune_events(conn, event_days=7, limit=2) == 2
        assert retention.auto_archive(conn, idle_days=7, limit=2) == 2
        for cid in threads:
            _age_archive(conn, cid, 400)
        assert retention.delete_archived(conn, archived_days=30, limit=1) == 1
        assert retention.prune_old_executions(conn, event_days=7, limit=1,
                                              max_rows=2) == 2
    finally:
        _drop(conn, *orphans)
        for cid in threads:
            _wipe(conn, cid)
            retention.delete_tree(conn, cid)


def test_one_long_thread_cannot_exceed_the_run_row_budget(conn, world):
    """CODE-8. The conversation cap capped conversations, not rows: one thread with a
    million expired events still went in ONE transaction. The budget is on rows, the
    oldest first, and the watermark names exactly what went - so the next run resumes."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    ids = [_event(conn, cid, age_days=400) for _ in range(5)]
    try:
        assert retention.prune_events(conn, event_days=7, max_rows=2) == 2
        assert _event_ids(conn, cid) == ids[2:]
        assert _watermark(conn, cid) == ids[1]

        # What this run left, the next run takes - oldest first, nothing skipped.
        assert retention.prune_events(conn, event_days=7, max_rows=10) == 3
        assert _event_ids(conn, cid) == []
        assert _watermark(conn, cid) == ids[4]
    finally:
        _wipe(conn, cid)
        retention.delete_tree(conn, cid)
