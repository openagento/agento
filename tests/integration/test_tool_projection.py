"""`tool.called` — projecting the toolbox's audit into a thread (PRD E3-E5 §6.4.2).

The audit is written by Node, which knows nothing about conversations. This projection is
the only bridge, and `tool_invocation.conversation_relayed_at` is its checkpoint: an event
that §10.1 later prunes must not be projected again.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from agento.modules.conversation.src import relay, service

from .conftest import _clean  # noqa: F401
from .test_conversation_submission import _job_type  # noqa: F401


@pytest.fixture(autouse=True)
def _clean_projection_tables(conn):
    def wipe():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tool_invocation")
            cur.execute("DELETE FROM execution")
        conn.commit()
    wipe()
    yield
    wipe()


@pytest.fixture
def thread(conn, world):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    message_id, job_id, _ = service.submit_message(
        conn, conversation_id=cid, user_id=world["owner"].id,
        client_message_id="c1", content="pytanie")
    return {"conversation_id": cid, "message_id": message_id, "job_id": job_id,
            "agent_view_id": world["view"]}


def _execution(conn, job_id, *, execution_id=None, attempt=1) -> str:
    execution_id = execution_id or str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            # The thread is linked at claim (E9 §3.5); a hand-made run links it here.
            "INSERT INTO execution (execution_id, job_id, attempt, status, conversation_id) "
            "VALUES (%s, %s, %s, 'running', "
            "        (SELECT conversation_id FROM message WHERE job_id = %s LIMIT 1))",
            (execution_id, job_id, attempt, job_id))
    conn.commit()
    return execution_id


def _invocation(conn, *, run_execution_id, tool_name="outlook_reply",
                outcome="ok", agent_view_id=None, capability_id=1,
                invocation_id=None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tool_invocation (id, execution_id, run_execution_id, capability_id, "
            "transport, actor, subject_id, tool_name, args_sha256, agent_view_id, "
            "workspace_id, outcome) "
            "VALUES (%s, %s, %s, %s, 'http', 'agent', '7', %s, %s, %s, 3, %s)",
            (invocation_id, str(uuid.uuid4()), run_execution_id, capability_id,
             tool_name, "a" * 64, agent_view_id, outcome),
        )
        row_id = invocation_id or cur.lastrowid
    conn.commit()
    return row_id


def _events(conn, kind=None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM conversation_event ORDER BY id")
        rows = list(cur.fetchall())
    conn.commit()
    return [r for r in rows if kind is None or r["kind"] == kind]


def _checkpoints(conn) -> list[tuple[int, object]]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, conversation_relayed_at FROM tool_invocation ORDER BY id")
        rows = [(r["id"], r["conversation_relayed_at"]) for r in cur.fetchall()]
    conn.commit()
    return rows


# --- the projection ------------------------------------------------------

def test_a_conversation_tool_call_projects_one_event(conn, thread):
    execution_id = _execution(conn, thread["job_id"])
    invocation = _invocation(conn, run_execution_id=execution_id,
                             agent_view_id=thread["agent_view_id"])

    assert relay.project_tool_calls(conn) == 1

    events = _events(conn, "tool.called")
    assert len(events) == 1
    assert events[0]["conversation_id"] == thread["conversation_id"]
    assert events[0]["source_kind"] == "tool_invocation"
    assert events[0]["source_id"] == invocation
    assert events[0]["execution_id"] == execution_id


def test_the_payload_carries_the_name_scope_and_outcome_and_no_argument(conn, thread):
    execution_id = _execution(conn, thread["job_id"])
    invocation = _invocation(conn, run_execution_id=execution_id, tool_name="outlook_reply",
                             outcome="ok", agent_view_id=thread["agent_view_id"])

    relay.project_tool_calls(conn)

    payload = json.loads(_events(conn, "tool.called")[0]["payload"])
    assert payload == {
        "tool_name": "outlook_reply",
        "outcome": "ok",
        "tool_invocation_id": invocation,
        "execution_id": execution_id,
        "agent_view_id": thread["agent_view_id"],
        "workspace_id": 3,
    }
    # The audit itself only ever held a digest of the arguments; this is one hop closer to a
    # browser than the audit is, so nothing resembling one may appear.
    assert "a" * 64 not in json.dumps(payload)
    assert "args" not in payload and "result" not in payload


def test_a_purged_capability_still_projects(conn, thread):
    """The join is through `execution`, never `toolbox_capability`: capability rows are
    purged and the audit outlives them."""
    execution_id = _execution(conn, thread["job_id"])
    _invocation(conn, run_execution_id=execution_id, capability_id=None,
                agent_view_id=thread["agent_view_id"])

    assert relay.project_tool_calls(conn) == 1
    assert len(_events(conn, "tool.called")) == 1


def test_a_refused_call_projects_with_its_outcome(conn, thread):
    execution_id = _execution(conn, thread["job_id"])
    _invocation(conn, run_execution_id=execution_id, outcome="unauthorized")

    relay.project_tool_calls(conn)

    assert json.loads(_events(conn, "tool.called")[0]["payload"])["outcome"] == "unauthorized"


def test_a_call_still_running_is_left_for_the_next_tick(conn, thread):
    """`pending` is not an outcome. Projecting the guess would freeze it into an
    append-only row; the dispatcher finalizes every exit, so waiting one tick is free."""
    execution_id = _execution(conn, thread["job_id"])
    invocation = _invocation(conn, run_execution_id=execution_id, outcome="pending")

    assert relay.project_tool_calls(conn) == 0
    assert _checkpoints(conn) == [(invocation, None)]

    with conn.cursor() as cur:
        cur.execute("UPDATE tool_invocation SET outcome = 'ok' WHERE id = %s", (invocation,))
    conn.commit()

    assert relay.project_tool_calls(conn) == 1
    assert json.loads(_events(conn, "tool.called")[0]["payload"])["outcome"] == "ok"


def test_a_call_with_no_run_is_never_looked_at(conn, thread):
    """An interactive or service call carries no run, so it belongs to no thread. It keeps a
    NULL checkpoint rather than being marked: there is nothing to project, ever."""
    _invocation(conn, run_execution_id=None)

    assert relay.project_tool_calls(conn) == 0
    assert _events(conn, "tool.called") == []


def test_a_non_conversation_run_is_checkpointed_with_no_event(conn, world):
    """Classification is by `job.source`, exactly as the outbox relay's."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job (source, type, agent_type, status, reference_id, prompt, "
            "idempotency_key) VALUES ('jira', 'blank', 'claude', 'TODO', 'PROJ-1', 'p', %s)",
            (f"tp:{uuid.uuid4()}",))
        job_id = cur.lastrowid
    conn.commit()
    execution_id = _execution(conn, job_id)
    invocation = _invocation(conn, run_execution_id=execution_id)

    assert relay.project_tool_calls(conn) == 1

    assert _events(conn, "tool.called") == []
    assert _checkpoints(conn)[0][0] == invocation
    assert _checkpoints(conn)[0][1] is not None


# --- the checkpoint, and the two namespaces ------------------------------

def test_pruning_the_event_does_not_recreate_it_on_the_next_tick(conn, thread):
    """The checkpoint lives on the producer row. Read off the event table it would be
    §10.1's prune that resurrects every `tool.called` it just deleted."""
    execution_id = _execution(conn, thread["job_id"])
    _invocation(conn, run_execution_id=execution_id)
    relay.project_tool_calls(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM conversation_event WHERE kind = 'tool.called'")
    conn.commit()

    assert relay.project_tool_calls(conn) == 0
    assert _events(conn, "tool.called") == []


def test_the_projection_is_idempotent_under_a_re_run(conn, thread):
    execution_id = _execution(conn, thread["job_id"])
    _invocation(conn, run_execution_id=execution_id)
    relay.project_tool_calls(conn)
    before = _events(conn)

    assert relay.project_tool_calls(conn) == 0
    assert _events(conn) == before


def test_a_checkpoint_rewound_yields_no_second_event(conn, thread):
    """A crash between the insert and the checkpoint re-runs the batch; the unique key on
    `(source_kind, source_id)` absorbs it."""
    execution_id = _execution(conn, thread["job_id"])
    _invocation(conn, run_execution_id=execution_id)
    relay.project_tool_calls(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE tool_invocation SET conversation_relayed_at = NULL")
    conn.commit()

    assert relay.project_tool_calls(conn) == 1
    assert len(_events(conn, "tool.called")) == 1


def test_a_message_id_and_a_tool_invocation_id_of_the_same_value_both_project(conn, thread):
    """One namespace per producer: `source_kind` is what keeps two local ids apart."""
    execution_id = _execution(conn, thread["job_id"])
    _invocation(conn, run_execution_id=execution_id, invocation_id=thread["message_id"])

    relay.project_tool_calls(conn)

    same_id = [e for e in _events(conn) if e["source_id"] == thread["message_id"]]
    assert {e["source_kind"] for e in same_id} == {"message", "tool_invocation"}
    assert len(same_id) == 2


def test_the_two_relays_do_not_claim_each_others_rows(conn, thread):
    execution_id = _execution(conn, thread["job_id"])
    _invocation(conn, run_execution_id=execution_id)

    assert relay.relay_outbox(conn) == 1          # only the turn's `job.queued`
    assert relay.project_tool_calls(conn) == 1    # only the tool call

    kinds = [e["kind"] for e in _events(conn)]
    assert "job.queued" in kinds and "tool.called" in kinds


def test_calls_project_in_tool_invocation_id_order(conn, thread):
    execution_id = _execution(conn, thread["job_id"])
    ids = [_invocation(conn, run_execution_id=execution_id, tool_name=f"t{i}")
           for i in range(3)]

    relay.project_tool_calls(conn)

    assert [e["source_id"] for e in _events(conn, "tool.called")] == ids


def test_a_batch_beyond_the_limit_leaves_the_rest_unchecked(conn, thread):
    execution_id = _execution(conn, thread["job_id"])
    ids = [_invocation(conn, run_execution_id=execution_id) for _ in range(3)]

    assert relay.project_tool_calls(conn, limit=2) == 2
    assert [i for i, mark in _checkpoints(conn) if mark is None] == ids[2:]

    assert relay.project_tool_calls(conn, limit=2) == 1
    assert [e["source_id"] for e in _events(conn, "tool.called")] == ids


# --- the boundary --------------------------------------------------------

def test_no_node_code_writes_a_conversation_event():
    """The toolbox knows nothing about conversations, and this projection is the whole of
    the conversation's knowledge of the audit. A Node writer would be a second producer
    outside the ordering and outside the idempotency key."""
    found = subprocess.run(
        ["grep", "-rn", "conversation_event", "src/agento/toolbox/"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2],
    )
    assert found.stdout == "", found.stdout
