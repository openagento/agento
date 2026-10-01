"""§5.3's finalizer, implemented (PRD E3-E5 §6.4.2).

Task 12 shipped the protocol and its nine call sites with nothing behind them - which is
the module-disabled path. This is the implementation, and these tests assert what it writes
in the framework's own transaction.
"""
from __future__ import annotations

import json
import uuid

import pytest

from agento.framework.execution_hooks import clear as clear_hooks
from agento.framework.execution_hooks import (
    finalize_execution,
    register_execution_finalizer,
)
from agento.modules.conversation.src import relay, service
from agento.modules.conversation.src.finalizer import (
    TRUNCATION_MARKER,
    ConversationFinalizer,
    truncate_utf8,
)

from .conftest import (
    _clean,  # noqa: F401
    bootstrap_for_tests,
)
from .test_conversation_submission import _job_type  # noqa: F401


@pytest.fixture(autouse=True)
def finalizer():
    clear_hooks()
    register_execution_finalizer(ConversationFinalizer(), module="conversation")
    yield
    # The registry is global: leave the shipped registrations behind us, or every
    # later test in the session runs with the seams empty.
    clear_hooks()
    bootstrap_for_tests()


@pytest.fixture(autouse=True)
def _clean_executions(conn):
    def wipe():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM execution")
        conn.commit()
    wipe()
    yield
    wipe()


@pytest.fixture
def turn(conn, world):
    """A submitted turn with an open execution, as the consumer would leave it."""
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    message_id, job_id, _ = service.submit_message(
        conn, conversation_id=conversation_id, user_id=world["owner"].id,
        client_message_id="c1", content="pytanie")
    execution_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO execution (execution_id, job_id, attempt, status) "
                    "VALUES (%s, %s, 1, 'running')", (execution_id, job_id))
        cur.execute("UPDATE job SET status = 'RUNNING' WHERE id = %s", (job_id,))
        cur.execute("DELETE FROM job_event_outbox")
    conn.commit()
    return {"conversation_id": conversation_id, "message_id": message_id,
            "job_id": job_id, "execution_id": execution_id}


def _answered(conn, job_id, output="Odpowiedź asystenta.") -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET output = %s WHERE id = %s", (output, job_id))
    conn.commit()


def _finalize(conn, turn, *, outcome="succeeded", job_terminal=True,
              execution_id=..., attempt=1) -> None:
    """Call the seam the way the consumer does: its own connection, its own commit."""
    finalize_execution(
        conn=conn, job_id=turn["job_id"], attempt=attempt,
        execution_id=turn["execution_id"] if execution_id is ... else execution_id,
        outcome=outcome, job_terminal=job_terminal)
    conn.commit()


def _messages(conn, conversation_id) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM message WHERE conversation_id = %s ORDER BY id",
                    (conversation_id,))
        rows = list(cur.fetchall())
    conn.commit()
    return rows


def _outbox(conn, kind=None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM job_event_outbox ORDER BY id")
        rows = list(cur.fetchall())
    conn.commit()
    return [r for r in rows if kind is None or r["kind"] == kind]


def _execution(conn, execution_id) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM execution WHERE execution_id = %s", (execution_id,))
        row = cur.fetchone()
    conn.commit()
    return row


# --- the assistant row ----------------------------------------------------

def test_the_finalizer_writes_the_assistant_row_and_its_event(conn, turn):
    _answered(conn, turn["job_id"], "Odpowiedź asystenta.")

    _finalize(conn, turn)

    rows = _messages(conn, turn["conversation_id"])
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[1]["content"] == "Odpowiedź asystenta."
    assert rows[1]["execution_id"] == turn["execution_id"]
    events = _outbox(conn, "assistant.message")
    assert len(events) == 1
    assert json.loads(events[0]["payload"])["message_id"] == rows[1]["id"]


def test_the_assistant_row_carries_no_job_id_and_no_job_state(conn, turn):
    """The reply is not itself a queued turn. A second row carrying the same `job_id` would
    make any join on it return two rows, and a `job_state` here would make §4.4 see a
    second open turn."""
    _answered(conn, turn["job_id"])

    _finalize(conn, turn)

    assistant = _messages(conn, turn["conversation_id"])[1]
    assert assistant["job_id"] is None
    assert assistant["job_state"] is None


def test_a_fresh_sessions_assembled_history_contains_the_answer(conn, turn, world):
    """Without this row §4.2 would rebuild a thread of questions with no replies."""
    _answered(conn, turn["job_id"], "Pierwsza odpowiedź.")
    _finalize(conn, turn)
    second_message, _, _ = service.submit_message(
        conn, conversation_id=turn["conversation_id"], user_id=world["owner"].id,
        client_message_id="c2", content="drugie pytanie")

    from agento.modules.conversation.src.workflow import load_turns

    history = load_turns(conn, turn["conversation_id"], second_message)
    conn.commit()
    assert [t["role"] for t in history] == ["user", "assistant", "user"]
    assert history[1]["content"] == "Pierwsza odpowiedź."


def test_a_run_with_no_output_writes_no_assistant_row(conn, turn):
    _finalize(conn, turn)

    assert [r["role"] for r in _messages(conn, turn["conversation_id"])] == ["user"]
    assert _outbox(conn, "assistant.message") == []


def test_a_failed_attempt_writes_no_assistant_row(conn, turn):
    _answered(conn, turn["job_id"], "cokolwiek")

    _finalize(conn, turn, outcome="failed", job_terminal=True)

    assert [r["role"] for r in _messages(conn, turn["conversation_id"])] == ["user"]


# --- idempotency: all three counts ---------------------------------------

def test_a_replayed_terminal_transaction_leaves_one_row_one_event_and_one_relay(
    conn, turn
):
    """Three counts, not one. The message row is pinned by `(conversation_id,
    execution_id)`; an outbox row is a fresh id every time. Writing the event beside the
    insert rather than inside its win would deliver the same answer to the thread twice."""
    _answered(conn, turn["job_id"], "Jedyna odpowiedź.")

    _finalize(conn, turn)
    _finalize(conn, turn)

    assert len([r for r in _messages(conn, turn["conversation_id"])
                if r["role"] == "assistant"]) == 1
    assert len(_outbox(conn, "assistant.message")) == 1

    relay.relay_outbox(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM conversation_event WHERE kind = %s",
                    ("assistant.message",))
        assert cur.fetchone()["n"] == 1
    conn.commit()


def test_the_event_survives_a_failing_observer(conn, turn):
    """The finalizer writes it, not an observer - and an observer is swallowed (F22)."""
    from agento.framework.event_manager import ObserverEntry, clear, get_event_manager

    clear()

    def boom(self, event):
        raise RuntimeError("observer down")

    get_event_manager().register("execution_finish_after", ObserverEntry(
        name="boom", observer_class=type("Boom", (), {"execute": boom})))
    try:
        _answered(conn, turn["job_id"], "Odpowiedź mimo wszystko.")
        _finalize(conn, turn)
        get_event_manager().dispatch("execution_finish_after", object())
    finally:
        clear()

    assert len(_outbox(conn, "assistant.message")) == 1


# --- `message.job_state`, per §3.2's rule as an argument ------------------

def test_job_state_becomes_terminal_only_when_the_job_is_terminal(conn, turn):
    _finalize(conn, turn, outcome="failed", job_terminal=False)
    assert _messages(conn, turn["conversation_id"])[0]["job_state"] == "published"

    _finalize(conn, turn, outcome="failed", job_terminal=True)
    assert _messages(conn, turn["conversation_id"])[0]["job_state"] == "terminal"


def test_job_state_stays_published_across_a_retry(conn, turn):
    """The turn is not over: §4.4 must keep deferring the next one."""
    _finalize(conn, turn, outcome="failed", job_terminal=False)

    assert _messages(conn, turn["conversation_id"])[0]["job_state"] == "published"


def test_job_state_stays_published_across_stale_recovery(conn, turn):
    _finalize(conn, turn, outcome="abandoned", job_terminal=False, execution_id=None)

    assert _messages(conn, turn["conversation_id"])[0]["job_state"] == "published"


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_a_terminal_job_marks_the_turn_terminal(conn, turn, outcome):
    _finalize(conn, turn, outcome=outcome, job_terminal=True)

    assert _messages(conn, turn["conversation_id"])[0]["job_state"] == "terminal"


# --- the execution row ---------------------------------------------------

def test_the_execution_status_is_written_on_every_call(conn, turn):
    _finalize(conn, turn, outcome="failed", job_terminal=False)

    assert _execution(conn, turn["execution_id"])["status"] == "failed"
    assert _execution(conn, turn["execution_id"])["finished_at"] is not None


def test_stale_recovery_marks_the_execution_abandoned_not_running(conn, turn):
    """The recovery paths never held the id, so the row is found by `(job_id, attempt)`."""
    _finalize(conn, turn, outcome="abandoned", job_terminal=False, execution_id=None)

    assert _execution(conn, turn["execution_id"])["status"] == "abandoned"


def test_a_second_close_does_not_reopen_or_overwrite_the_outcome(conn, turn):
    _finalize(conn, turn, outcome="succeeded", job_terminal=True)

    _finalize(conn, turn, outcome="abandoned", job_terminal=False)

    assert _execution(conn, turn["execution_id"])["status"] == "succeeded"


def test_a_recovery_with_no_open_execution_writes_nothing_and_does_not_raise(conn, turn):
    with conn.cursor() as cur:
        cur.execute("UPDATE execution SET status = 'succeeded'")
    conn.commit()

    _finalize(conn, turn, outcome="abandoned", job_terminal=False, execution_id=None)

    assert [r["role"] for r in _messages(conn, turn["conversation_id"])] == ["user"]


# --- it is not the framework's writer ------------------------------------

def test_the_finalizer_never_writes_job_failed(conn, turn):
    """`job.failed` is a framework job transition. A module that owned it would silently
    drop it from the module-disabled guarantee."""
    _finalize(conn, turn, outcome="failed", job_terminal=True)

    assert _outbox(conn, "job.failed") == []


def test_a_non_conversation_job_is_left_alone(conn, world):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job (source, type, agent_type, status, reference_id, prompt, "
            "idempotency_key, output) VALUES ('jira', 'blank', 'claude', 'RUNNING', "
            "'PROJ-1', 'p', %s, 'wynik')", (f"fin:{uuid.uuid4()}",))
        job_id = cur.lastrowid
        execution_id = str(uuid.uuid4())
        cur.execute("INSERT INTO execution (execution_id, job_id, attempt, status) "
                    "VALUES (%s, %s, 1, 'running')", (execution_id, job_id))
    conn.commit()

    finalize_execution(conn=conn, job_id=job_id, attempt=1, execution_id=execution_id,
                       outcome="succeeded", job_terminal=True)
    conn.commit()

    # Its execution still closes - that row is the module's and belongs to the attempt.
    assert _execution(conn, execution_id)["status"] == "succeeded"
    assert _outbox(conn, "assistant.message") == []


# --- Review Focus 3: the byte bound, on a codepoint boundary -------------

def test_truncation_cuts_on_a_codepoint_boundary_and_appends_the_marker():
    """Every character here is 2 bytes, so a byte budget that is odd would land mid-
    codepoint. The result must still be valid UTF-8."""
    text = "ą" * 100
    limit = 41 + len(TRUNCATION_MARKER.encode("utf-8"))

    cut = truncate_utf8(text, limit)

    assert cut.endswith(TRUNCATION_MARKER)
    assert len(cut.encode("utf-8")) <= limit
    assert cut.encode("utf-8").decode("utf-8") == cut
    assert "�" not in cut


def test_text_inside_the_bound_is_untouched():
    assert truncate_utf8("krótka odpowiedź", 32768) == "krótka odpowiedź"


def test_a_bound_smaller_than_the_marker_still_yields_valid_utf8():
    cut = truncate_utf8("ą" * 100, 2)

    assert len(cut.encode("utf-8")) <= 2
    assert cut.encode("utf-8").decode("utf-8") == cut


def test_a_long_answer_is_truncated_not_rejected_and_round_trips_as_utf8(conn, turn):
    """The run already happened; its answer must land."""
    limit = service.config(conn, "limits/max_message_bytes")
    _answered(conn, turn["job_id"], "ą" * (limit + 500))

    _finalize(conn, turn)

    assistant = _messages(conn, turn["conversation_id"])[1]
    assert len(assistant["content"].encode("utf-8")) <= limit
    assert assistant["content"].endswith(TRUNCATION_MARKER)
    assert "�" not in assistant["content"]
    # Through the API, as JSON, exactly as the panel reads it.
    rendered = json.dumps({"content": assistant["content"]})
    assert json.loads(rendered)["content"] == assistant["content"]
    # The event payload is bounded by the same rule.
    payload = json.loads(_outbox(conn, "assistant.message")[0]["payload"])
    assert payload["content"] == assistant["content"]
