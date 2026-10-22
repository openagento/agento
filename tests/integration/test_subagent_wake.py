"""The parent wake (`subagent/wake_parent`, plan §8.3) against MySQL.

Drives the real publisher and the real `WakeParentObserver` (as the consumer dispatches it
after a terminal status commits), and the insert lock order of a delegated job.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pymysql
import pytest

from agento.framework.event_manager import get_event_manager
from agento.framework.events import JobBlockedEvent, JobDeadEvent, JobSucceededEvent
from agento.framework.job_models import Job
from agento.framework.job_types import resolve_job_type
from agento.framework.publish_service import publish_job
from agento.framework.publisher import insert_job
from agento.modules.subagent.src.observers import WakeParentObserver, maybe_wake, wake_key

from .conftest import _test_connection, bootstrap_for_tests


@pytest.fixture(autouse=True)
def _registered():
    bootstrap_for_tests()


def _sql(sql: str, params=()) -> list[dict]:
    c = _test_connection(autocommit=True)
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        c.close()


@pytest.fixture
def views() -> tuple[int, int, int]:
    ids = []
    _sql("INSERT IGNORE INTO workspace (code, label) VALUES ('wk_ws', 't')")
    ws = _sql("SELECT id FROM workspace WHERE code = 'wk_ws'")[0]["id"]
    for code in ("wk_a", "wk_b", "wk_c"):
        _sql("INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, %s, 't')", (ws, code))
        ids.append(_sql("SELECT id FROM agent_view WHERE code = %s", (code,))[0]["id"])
    return ids[0], ids[1], ids[2]


def _wake_off(view: int) -> None:
    _sql("INSERT INTO core_config_data (scope, scope_id, path, value, encrypted) "
         "VALUES ('agent_view', %s, 'subagent/wake_parent', '0', 0)", (view,))


_keys = iter(range(10**6))


def _root(view: int, reference: str = "AI-1") -> int:
    return publish_job(source="jira", agent_type=resolve_job_type("todo"), agent_view_id=view,
                       reference_id=reference, idempotency_key=f"wk:{next(_keys)}",
                       requester=None, priority=40)


def _row(job_id: int) -> dict:
    return _sql("SELECT * FROM job WHERE id = %s", (job_id,))[0]


def _child(parent: int, view: int | None = None, line: int | None = None) -> int:
    p = _row(parent)
    return publish_job(source="subagent", agent_type=resolve_job_type("subagent"),
                       agent_view_id=view or p["agent_view_id"], reference_id=str(parent),
                       idempotency_key=f"wk:{next(_keys)}", requester=None, priority=40,
                       context="task", thread_id=p["thread_id"], parent_job_id=parent,
                       depth=p["depth"] + 1, line_id=line)


def _set(job_id: int, status: str) -> None:
    _sql("UPDATE job SET status = %s WHERE id = %s", (status, job_id))


_EVENTS = {"SUCCESS": lambda j: JobSucceededEvent(job=j),
           "FAILED": lambda j: JobBlockedEvent(job=j, error=RuntimeError("blocked")),
           "DEAD": lambda j: JobDeadEvent(job=j, error=RuntimeError("dead"))}


def _end(job_id: int, status: str = "SUCCESS") -> None:
    """Commit the terminal status, then run the observer as the consumer does after it."""
    _set(job_id, status)
    WakeParentObserver().execute(_EVENTS[status](Job.from_row(_row(job_id))))


def _wakes() -> list[dict]:
    return _sql("SELECT * FROM job WHERE idempotency_key LIKE 'subagent\\_wake:%%' ORDER BY id")


def test_the_observer_is_registered_on_the_three_terminal_events():
    em = get_event_manager()
    for name in ("job_succeed_after", "job_blocked_after", "job_dead_after"):
        assert WakeParentObserver in {e.observer_class for e in em._observers.get(name, [])}
    assert WakeParentObserver not in {e.observer_class for e in em._observers.get("job_retry_after", [])}


def test_five_children_wake_the_parent_once_when_the_last_one_ends(views):
    a, b, _ = views
    parent = _root(a)
    children = [_child(parent, view=b if i == 4 else None) for i in range(5)]
    _end(parent)                                   # the parent ends first: children still open
    for c in children[:4]:
        _end(c)
        assert _wakes() == []
    _end(children[4], "DEAD")

    [wake] = _wakes()
    p = _row(parent)
    assert (wake["type"], wake["source"], wake["reference_id"], wake["agent_view_id"],
            wake["priority"], wake["thread_id"], wake["parent_job_id"], wake["depth"],
            wake["line_id"], wake["idempotency_key"], wake["status"]) == (
        "followup", "jira", "AI-1", a, 40, p["thread_id"], None, 0, None,
        f"subagent_wake:{parent}", "TODO")
    assert wake["context"].splitlines() == [
        "Sub-agents you started have finished:",
        *[f"#{c} (wk_a) SUCCESS" for c in children[:4]], f"#{children[4]} (wk_b) DEAD",
        "Read each result with agent_result."]

    _end(children[4], "DEAD")                      # a late duplicate observer
    assert len(_wakes()) == 1


def test_children_that_end_at_once_publish_one_wake(views):
    parent = _root(views[0])
    _set(parent, "SUCCESS")
    children = [_child(parent) for _ in range(5)]
    barrier = threading.Barrier(len(children))
    errors: list[BaseException] = []

    def finish(job_id: int) -> None:
        barrier.wait()
        try:
            _end(job_id)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    workers = [threading.Thread(target=finish, args=(c,)) for c in children]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=60)
    assert errors == []
    assert [w["idempotency_key"] for w in _wakes()] == [wake_key(parent)]


@pytest.mark.parametrize("parent_status", ["RUNNING", "FAILED", "DEAD"])
def test_a_parent_that_did_not_succeed_is_not_woken(views, parent_status):
    parent = _root(views[0])
    child = _child(parent)
    _set(parent, parent_status)
    _end(child)
    assert _wakes() == []


@pytest.mark.parametrize("open_status", ["TODO", "RUNNING", "PAUSED"])
def test_an_open_row_anywhere_in_the_subtree_holds_the_wake(views, open_status):
    parent = _root(views[0])
    _set(parent, "SUCCESS")
    done, deep = _child(parent), _child(parent)
    grandchild = _child(deep)
    _set(deep, "SUCCESS")
    _set(grandchild, open_status)                  # a retried child is TODO again
    _end(done)
    assert maybe_wake(_test_connection(), parent) is None
    assert _wakes() == []


def test_wake_parent_off_for_the_parents_view_publishes_nothing(views):
    a, _, _ = views
    _wake_off(a)
    parent = _root(a)
    _set(parent, "SUCCESS")
    _end(_child(parent))
    assert _wakes() == []


def test_a_row_with_no_child_is_not_woken(views):
    parent = _root(views[0])
    _end(parent)
    assert _wakes() == []


def test_a_child_due_for_its_own_wake_holds_the_parent_until_that_wake_ends(views):
    """P -> C -> G, with C's observer late (the race of plan §8.3): G's end wakes C; P is
    woken only after C's wake turn ends, and its prompt names that turn."""
    parent = _root(views[0])
    _set(parent, "SUCCESS")
    c = _child(parent)
    g = _child(c)
    _set(c, "SUCCESS")                             # C ended; its observer has not run yet
    assert maybe_wake(_test_connection(), parent) is None   # G still open
    _end(g)

    [wake_c] = _wakes()
    assert (wake_c["idempotency_key"], wake_c["parent_job_id"], wake_c["depth"],
            wake_c["line_id"], wake_c["source"], wake_c["reference_id"]) == (
        wake_key(c), parent, 1, c, "subagent", str(parent))
    _end(c)                                        # C's late observer: C's wake is open
    assert len(_wakes()) == 1

    _set(wake_c["id"], "RUNNING")
    _end(wake_c["id"])
    wake_p = _wakes()[-1]
    assert wake_p["idempotency_key"] == wake_key(parent)
    assert f"#{wake_c['id']} (wk_a) SUCCESS" in wake_p["context"]
    assert f"#{c} " not in wake_p["context"]       # the line's latest turn, not its first


def test_a_settled_child_with_no_wake_row_yet_is_pending(views):
    parent = _root(views[0])
    _set(parent, "SUCCESS")
    c = _child(parent)
    _set(_child(c), "SUCCESS")
    _set(c, "SUCCESS")
    assert maybe_wake(_test_connection(), parent) is None
    assert _wakes() == []


def test_a_child_whose_view_does_not_wake_is_not_pending(views):
    a, b, _ = views
    _wake_off(b)
    parent = _root(a)
    _set(parent, "SUCCESS")
    c = _child(parent, view=b)
    _set(_child(c), "SUCCESS")
    _end(c)
    assert [w["idempotency_key"] for w in _wakes()] == [wake_key(parent)]


@pytest.mark.parametrize("case", ["child dead", "child view off"])
def test_a_child_that_ends_before_its_own_children_and_is_not_woken_still_wakes_the_parent(
        views, case):
    """P -> C -> G: C ends while G is open, and C is never woken itself. G's end is the last
    event of P's subtree, so it must reach P (the observer walks every ancestor)."""
    a, b, _ = views
    if case == "child view off":
        _wake_off(b)
    parent = _root(a)
    _set(parent, "SUCCESS")
    c = _child(parent, view=b)
    g = _child(c)
    _end(c, "DEAD" if case == "child dead" else "SUCCESS")
    assert _wakes() == []
    _end(g)
    assert [w["idempotency_key"] for w in _wakes()] == [wake_key(parent)]


def test_a_great_grandchild_end_wakes_the_root(views):
    parent = _root(views[0])
    _set(parent, "SUCCESS")
    c = _child(parent)
    g = _child(c)
    gg = _child(g)
    _end(c, "DEAD")
    _end(g, "DEAD")
    _end(gg)
    assert [w["idempotency_key"] for w in _wakes()] == [wake_key(parent)]


@pytest.mark.parametrize("missing", ["agent_view_id", "reference_id"])
def test_a_settled_child_that_cannot_be_woken_does_not_hold_the_parent(views, missing):
    """A child with no view (deleted) or no reference gets no wake, so it is never due."""
    parent = _root(views[0])
    _set(parent, "SUCCESS")
    c = _child(parent)
    _set(_child(c), "SUCCESS")
    _sql(f"UPDATE job SET {missing} = NULL WHERE id = %s", (c,))
    _end(c)
    assert [w["idempotency_key"] for w in _wakes()] == [wake_key(parent)]


def test_the_wake_of_a_continuation_continues_its_line(views):
    parent = _root(views[0])
    _set(parent, "SUCCESS")
    c = _child(parent)
    _set(c, "SUCCESS")
    again = _child(parent, line=c)                 # agent_run(job_id=c)
    _set(again, "SUCCESS")
    _end(_child(again))
    wake = next(w for w in _wakes() if w["idempotency_key"] == wake_key(again))
    assert (wake["line_id"], wake["parent_job_id"], wake["depth"]) == (c, parent, 1)


def test_a_full_chain_holds_the_documented_number_of_delegated_jobs(views):
    """docs/modules/subagent.md "Limits": with N = 1 a tree holds 3 `subagent` jobs and, at
    depth > 0, 2 wake jobs (N + 2N² + 2N³ = 5): every wake row counts, not only agent_run's."""
    root = _root(views[0])
    _set(root, "SUCCESS")
    c1 = _child(root)
    c2 = _child(c1)
    c3 = _child(c2)
    _set(c1, "SUCCESS")
    _set(c2, "SUCCESS")
    _end(c3)
    for _ in range(3):                             # each wake ends and wakes the row above
        for wake in _wakes():
            if wake["status"] == "TODO":
                _end(wake["id"])
    rows = _sql("SELECT type, depth FROM job WHERE thread_id = %s", (_row(root)["thread_id"],))
    deep = [r["type"] for r in rows if r["depth"] > 0]
    assert sorted(deep) == ["followup", "followup", "subagent", "subagent", "subagent"]
    assert [w["depth"] for w in _wakes()] == [2, 1, 0]
    doc = (Path(__file__).resolve().parents[2] / "docs" / "modules" / "subagent.md").read_text()
    assert "`N + 2N² + 2N³` of them at\n`depth > 0`" in doc


def _detach(job_id: int) -> int:
    """Move a row to a thread of its own, as a claim's detach does (an Outlook revocation)."""
    ref = f"detached:{job_id}:{next(_keys)}"
    _sql("INSERT INTO thread (agent_view_id, ref, ref_hash, `key`) "
         "VALUES (%s, %s, SHA1(%s), REPLACE(UUID(), '-', ''))",
         (_row(job_id)["agent_view_id"], ref, ref))
    thread = _sql("SELECT id FROM thread WHERE ref = %s", (ref,))[0]["id"]
    _sql("UPDATE job SET thread_id = %s WHERE id = %s", (thread, job_id))
    return thread


def test_a_detached_child_and_the_work_it_starts_leave_the_parents_wake(views):
    """The thread P waits for is P's own: a detached child, and the row it starts in its new
    thread, neither hold P's wake nor appear in its prompt (agent_result refuses them)."""
    parent = _root(views[0])
    _set(parent, "SUCCESS")
    kept, detached = _child(parent), _child(parent)
    _end(detached)
    _detach(detached)
    _child(detached)                               # more work, open, in the new thread
    _end(kept)
    [wake] = _wakes()
    assert wake["idempotency_key"] == wake_key(parent)
    assert f"#{detached} " not in wake["context"] and f"#{kept} (wk_a) SUCCESS" in wake["context"]


@pytest.mark.parametrize("detached_last", [True, False])
def test_a_detached_child_ending_last_or_first_gives_one_wake(views, detached_last):
    """K ends while D is queued; D detaches at its claim and ends. Whichever end is last
    wakes P, once: P judges in its own thread, where D no longer counts."""
    parent = _root(views[0])
    _set(parent, "SUCCESS")
    kept, detached = _child(parent), _child(parent)
    if detached_last:
        _end(kept)                                 # D is still queued in P's thread
        assert _wakes() == []
        _detach(detached)
        _end(detached)
    else:
        _detach(detached)
        _end(detached)                             # K is still open
        assert _wakes() == []
        _end(kept)
    _end(detached)                                 # a late duplicate observer
    [wake] = _wakes()
    assert wake["idempotency_key"] == wake_key(parent)
    assert f"#{detached} " not in wake["context"]


def test_a_detached_child_ending_last_wakes_the_row_above_a_parent_that_is_not_woken(views):
    """R -> P -> (K, D). P ended DEAD (never woken), K ended while D was queued, D detaches
    and ends last: the walk goes on past P, so R is woken."""
    root = _root(views[0])
    _set(root, "SUCCESS")
    parent = _child(root)
    kept, detached = _child(parent), _child(parent)
    _end(parent, "DEAD")
    _end(kept)
    assert _wakes() == []
    _detach(detached)
    _end(detached)
    assert [w["idempotency_key"] for w in _wakes()] == [wake_key(root)]


def test_the_end_of_work_under_a_detached_child_does_not_wake_the_thread_it_left(views):
    """P's only child was detached: the end of the row that child started walks no link out
    of its thread, so P (with no child left in its thread) gets no wake."""
    parent = _root(views[0])
    _set(parent, "SUCCESS")
    detached = _child(parent)
    _set(detached, "SUCCESS")
    thread = _detach(detached)
    _end(_child(detached))
    assert wake_key(parent) not in {w["idempotency_key"] for w in _wakes()}
    assert {w["thread_id"] for w in _wakes()} <= {thread}


@pytest.mark.parametrize("view_count", [1, 3])
def test_the_query_count_does_not_grow_with_the_subtree(views, selects, view_count):
    def build(n_lines: int) -> int:
        parent = _root(views[0], reference=f"AI-{next(_keys)}")
        _set(parent, "SUCCESS")
        for i in range(n_lines):
            c = _child(parent, view=views[i % view_count])
            _set(_child(c), "SUCCESS")
            _set(c, "SUCCESS")
            wake = publish_job(source="subagent", agent_type=resolve_job_type("followup"),
                               agent_view_id=views[i % view_count], reference_id=str(parent),
                               idempotency_key=wake_key(c), requester=None, priority=40,
                               context="w", thread_id=_row(c)["thread_id"],
                               parent_job_id=parent, depth=1, line_id=c)
            _set(wake, "SUCCESS")
        return parent

    small, large = build(view_count), build(10)
    counts = []
    for parent in (small, large):
        selects.clear()
        assert maybe_wake(_test_connection(), parent) is not None
        counts.append(len(selects))
    assert counts[0] == counts[1] > 0


# --- the insert lock order (job, then thread) --------------------------------------

def _claim_vs_insert(insert) -> list[BaseException]:
    """A claim holds the parent row (a retry) and then locks the thread, while another
    connection inserts a child of that parent in that thread. One lock order: no deadlock."""
    parent = _root(_sql("SELECT id FROM agent_view WHERE code = 'wk_a'")[0]["id"])
    thread_id = _row(parent)["thread_id"]
    claim = _test_connection()
    errors: list[BaseException] = []
    with claim.cursor() as cur:
        cur.execute("SET SESSION innodb_lock_wait_timeout = 10")
        cur.execute("SELECT id FROM job WHERE id = %s FOR UPDATE", (parent,))

        def run() -> None:
            c = _test_connection()
            try:
                with c.cursor() as icur:
                    icur.execute("SET SESSION innodb_lock_wait_timeout = 10")
                    insert(icur, parent, thread_id)
                c.commit()
            except BaseException as exc:
                errors.append(exc)
            finally:
                c.close()

        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(0.5)                            # the insert now waits on the parent row
        try:
            cur.execute("SELECT id FROM thread WHERE id = %s FOR UPDATE", (thread_id,))
            claim.commit()
        except BaseException as exc:
            errors.append(exc)
            claim.rollback()
        worker.join(timeout=30)
    claim.close()
    return errors


def test_a_python_insert_with_a_parent_does_not_deadlock_with_a_claim_of_that_parent(views):
    def insert(cur, parent, thread_id):
        insert_job(cur, agent_type=resolve_job_type("followup"), source="subagent",
                   agent_view_id=views[0], priority=40, reference_id=str(parent),
                   idempotency_key=f"wk:{next(_keys)}", max_attempts=3, requester=None,
                   thread_id=thread_id, parent_job_id=parent, depth=1)

    assert _claim_vs_insert(insert) == []


def test_a_toolbox_insert_with_a_parent_does_not_deadlock_with_a_claim_of_that_parent(views):
    """The toolbox INSERT (job-enqueue.js) takes no lock of its own: its foreign key checks
    lock the parent row before the thread row."""
    def insert(cur, parent, thread_id):
        cur.execute("INSERT INTO job (source, agent_view_id, priority, reference_id, context, "
                    "idempotency_key, type, max_attempts, parent_job_id, depth, thread_id, "
                    "status, attempt) VALUES ('subagent', %s, 40, %s, 'p', %s, 'subagent', 3, "
                    "%s, 1, %s, 'TODO', 0)",
                    (views[0], str(parent), f"wk:{next(_keys)}", parent, thread_id))

    assert _claim_vs_insert(insert) == []


def test_the_other_lock_order_does_deadlock(views):
    """The control: thread first, then the parent row, is the deadlock the order prevents."""
    def insert(cur, parent, thread_id):
        cur.execute("SELECT id FROM thread WHERE id = %s FOR UPDATE", (thread_id,))
        cur.execute("SELECT id FROM job WHERE id = %s FOR SHARE", (parent,))

    errors = _claim_vs_insert(insert)
    assert len(errors) == 1 and isinstance(errors[0], pymysql.err.OperationalError)
    assert errors[0].args[0] == 1213               # ER_LOCK_DEADLOCK
