"""Threads against MySQL: identity at insert, one open turn per line, the resume target and
the detach (framework/threads.py). Drives the real `Consumer._try_dequeue`.
"""
from __future__ import annotations

import logging
import threading

import pytest

from agento.framework.consumer import Consumer
from agento.framework.event_manager import ObserverEntry, get_event_manager
from agento.framework.job_models import AgentType
from agento.framework.publish_service import publish_job
from agento.modules.core.src.patches.assign_open_job_threads import AssignOpenJobThreads

from .conftest import _test_connection

CLAIM = "job_claim_before"


@pytest.fixture
def conn():
    c = _test_connection(autocommit=False)
    yield c
    c.close()


@pytest.fixture
def view(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES ('t_ws', 't')")
        cur.execute("SELECT id FROM workspace WHERE code = 't_ws'")
        ws = cur.fetchone()["id"]
        cur.execute("INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, 't_av', 't')", (ws,))
        cur.execute("SELECT id FROM agent_view WHERE code = 't_av'")
        view_id = cur.fetchone()["id"]
    conn.commit()
    return view_id


@pytest.fixture
def consumer(int_db_config, int_consumer_config) -> Consumer:
    return Consumer(int_db_config, int_consumer_config, logging.getLogger("test"))


@pytest.fixture
def detach_when():
    """Register a `job_claim_before` observer that detaches the jobs in a set."""
    em = get_event_manager()
    before = list(em._observers.get(CLAIM, []))
    ids: set[int] = set()

    class Detach:
        def execute(self, event) -> None:
            if event.job_id in ids:
                event.detach_thread = True

    em.register(CLAIM, ObserverEntry(name="detach", observer_class=Detach))
    yield ids
    em._observers[CLAIM] = before


_keys = iter(range(10**6))


def _publish(int_db_config, view, reference_id="AI-1", **kwargs) -> int:
    return publish_job(source="jira", agent_type=AgentType.TODO, agent_view_id=view,
                       reference_id=reference_id, idempotency_key=f"t:{next(_keys)}",
                       requester=None, priority=kwargs.pop("priority", 50),
                       config=int_db_config, **kwargs)


def _row(conn, job_id) -> dict:
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM job WHERE id = %s", (job_id,))
        row = cur.fetchone()
    conn.commit()
    return row


def _sql(conn, sql, params=()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = list(cur.fetchall())
    conn.commit()
    return rows


def _finish(conn, job_id, session_id="s", status="SUCCESS") -> None:
    _sql(conn, "UPDATE job SET status = %s, session_id = %s, finished_at = NOW(6) WHERE id = %s",
         (status, session_id, job_id))


def _release(conn) -> None:
    """Undo the backoff so the next claim sees a deferred job again."""
    _sql(conn, "UPDATE job SET scheduled_after = NOW() WHERE status = 'TODO'")


def test_jobs_of_one_issue_share_one_thread_and_another_issue_has_its_own(conn, int_db_config, view):
    same = [_publish(int_db_config, view) for _ in range(3)]
    other = _publish(int_db_config, view, reference_id="AI-2")

    threads = {_row(conn, j)["thread_id"] for j in same}
    assert len(threads) == 1 and None not in threads
    assert _row(conn, other)["thread_id"] not in threads
    [thread] = _sql(conn, "SELECT * FROM thread WHERE id = %s", (threads.pop(),))
    assert (thread["ref"], thread["agent_view_id"], len(thread["key"])) == ("jira:AI-1", view, 32)


def test_three_jira_jobs_on_one_issue_share_one_shared_dir(conn, consumer, int_db_config, view, tmp_path):
    """The claim reads the thread's key, and each run's `./shared` is the one thread dir: a
    file the first turn writes is there for the third (docs/architecture/threads.md)."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from agento.framework.run_preparation import materialize_run_workspace

    runtime = SimpleNamespace(agent_view=SimpleNamespace(id=view, code="t_av"),
                              workspace=SimpleNamespace(code="t_ws"), harness=None)
    artifacts = tmp_path / "artifacts"
    keys, links = set(), []
    with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(artifacts)):
        for turn in range(3):
            job_id = _publish(int_db_config, view)
            job = consumer._try_dequeue()
            assert job.id == job_id
            keys.add(job.thread_key)
            _home, run_dir = materialize_run_workspace(
                runtime, run_id=job.id, em=None, check_build=lambda *_: None,
                thread_key=job.thread_key)
            links.append(run_dir / "shared")
            (run_dir / "shared" / f"turn-{turn}.md").write_text(str(turn))
            _finish(conn, job_id)

    [key] = keys
    assert _sql(conn, "SELECT COUNT(*) AS n FROM thread WHERE `key` = %s", (key,)) == [{"n": 1}]
    assert [p.name for p in (artifacts / "_threads").iterdir()] == [key]
    assert {p.resolve() for p in links} == {(artifacts / "_threads" / key / "shared").resolve()}
    assert sorted(p.name for p in links[0].iterdir()) == ["turn-0.md", "turn-1.md", "turn-2.md"]


def test_a_job_with_no_reference_is_its_own_thread(conn, int_db_config, view):
    job_id = _publish(int_db_config, view, reference_id=None)
    [thread] = _sql(conn, "SELECT ref FROM thread WHERE id = %s", (_row(conn, job_id)["thread_id"],))
    assert thread["ref"] == f"job:{job_id}"


def test_two_first_jobs_of_a_new_thread_at_once_make_one_thread(conn, int_db_config, view):
    barrier = threading.Barrier(8)
    ids: list[int] = []
    errors: list[BaseException] = []

    def first_job():
        barrier.wait()
        try:
            ids.append(_publish(int_db_config, view, reference_id="AI-RACE"))
        except BaseException as exc:  # a deadlock would land here, not in a warning
            errors.append(exc)

    workers = [threading.Thread(target=first_job) for _ in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    assert errors == [] and len(ids) == 8
    assert len({_row(conn, j)["thread_id"] for j in ids}) == 1
    assert _sql(conn, "SELECT COUNT(*) AS n FROM thread")[0]["n"] == 1


def test_a_later_turn_waits_for_the_earlier_one_whatever_its_priority(conn, consumer, int_db_config, view):
    first = _publish(int_db_config, view, priority=10)
    second = _publish(int_db_config, view, priority=90)

    assert consumer._try_dequeue() is None                 # the higher-priority later turn waits
    assert consumer._try_dequeue().id == first
    _release(conn)
    assert consumer._try_dequeue() is None                 # still: the first turn is RUNNING
    _finish(conn, first)
    _release(conn)
    assert consumer._try_dequeue().id == second


def test_a_follow_up_scheduled_for_later_does_not_hold_the_thread(conn, consumer, int_db_config, view):
    later = _publish(int_db_config, view)
    _sql(conn, "UPDATE job SET scheduled_after = NOW() + INTERVAL 3 DAY WHERE id = %s", (later,))
    now = _publish(int_db_config, view)

    assert consumer._try_dequeue().id == now


def test_a_retry_that_backs_off_still_holds_the_thread(conn, consumer, int_db_config, view):
    retry = _publish(int_db_config, view)
    # As the claim and a failed attempt leave it: started, back in TODO, backing off.
    _sql(conn, "UPDATE job SET attempt = 1, started_at = NOW(), "
               "scheduled_after = NOW() + INTERVAL 1 HOUR WHERE id = %s", (retry,))
    _publish(int_db_config, view)

    assert consumer._try_dequeue() is None and consumer._claim_deferred


def test_a_deferred_earlier_turn_is_not_passed_when_the_line_frees(conn, consumer, int_db_config, view):
    running = _publish(int_db_config, view)
    assert consumer._try_dequeue().id == running
    first_waiting = _publish(int_db_config, view)
    second_waiting = _publish(int_db_config, view)
    assert consumer._try_dequeue() is None                   # both wait behind `running`
    _release(conn)
    assert consumer._try_dequeue() is None
    # The line frees while the first waiting turn is still backed off.
    _finish(conn, running)
    _sql(conn, "UPDATE job SET scheduled_after = NOW() + INTERVAL 1 HOUR WHERE id = %s", (first_waiting,))
    _sql(conn, "UPDATE job SET scheduled_after = NOW() WHERE id = %s", (second_waiting,))

    assert consumer._try_dequeue() is None                   # the second does not pass the first
    _release(conn)
    assert consumer._try_dequeue().id == first_waiting


def test_a_follow_up_that_comes_due_waits_for_the_turn_that_passed_it(
    conn, consumer, int_db_config, view,
):
    """One open turn per line, whatever the ids; the follow-up continues the turn that
    finished last, not the one with the next lower id."""
    first = _publish(int_db_config, view)
    assert consumer._try_dequeue().id == first
    _finish(conn, first, session_id="s-first")
    follow_up = _publish(int_db_config, view)
    _sql(conn, "UPDATE job SET scheduled_after = NOW() + INTERVAL 3 DAY WHERE id = %s", (follow_up,))
    passing = _publish(int_db_config, view)
    assert consumer._try_dequeue().id == passing
    _sql(conn, "UPDATE job SET scheduled_after = NOW() WHERE id = %s", (follow_up,))   # it comes due

    assert consumer._try_dequeue() is None                     # `passing` is RUNNING
    _finish(conn, passing, session_id="s-passing")
    _release(conn)

    job = consumer._try_dequeue()
    assert (job.id, job.resume_job_id) == (follow_up, passing)


def test_turns_that_finish_in_one_second_resume_in_finish_order_not_id_order(
    conn, consumer, int_db_config, view,
):
    """The higher id finishes first and the lower id 100 ms later, in the same second: the
    next turn continues the lower id. A seconds-only finished_at made it a tie that the id
    broke the wrong way."""
    first = _publish(int_db_config, view)
    assert consumer._try_dequeue().id == first
    _finish(conn, first, session_id="s-first")
    follow_up = _publish(int_db_config, view)
    _sql(conn, "UPDATE job SET scheduled_after = NOW() + INTERVAL 3 DAY WHERE id = %s", (follow_up,))
    passing = _publish(int_db_config, view)
    assert consumer._try_dequeue().id == passing
    _finish(conn, passing, session_id="s-passing")
    _sql(conn, "UPDATE job SET scheduled_after = NOW() WHERE id = %s", (follow_up,))
    assert consumer._try_dequeue().id == follow_up
    _finish(conn, follow_up, session_id="s-follow-up")
    _sql(conn, "UPDATE job SET finished_at = '2026-01-01 09:00:00' WHERE id = %s", (first,))
    _sql(conn, "UPDATE job SET finished_at = '2026-01-01 10:00:00.100000' WHERE id = %s", (passing,))
    _sql(conn, "UPDATE job SET finished_at = '2026-01-01 10:00:00.200000' WHERE id = %s", (follow_up,))
    assert follow_up < passing

    nxt = _publish(int_db_config, view)
    job = consumer._try_dequeue()
    assert (job.id, job.resume_job_id) == (nxt, follow_up)


def test_a_turn_waiting_for_its_credential_pool_is_not_passed(conn, consumer, int_db_config, view):
    """The pool wait refunds the attempt (attempt 0, scheduled later); it has started."""
    waiting = _publish(int_db_config, view)
    assert consumer._try_dequeue().id == waiting
    _sql(conn, "UPDATE job SET status = 'TODO', attempt = 0, "
               "scheduled_after = NOW() + INTERVAL 1 HOUR WHERE id = %s", (waiting,))
    _publish(int_db_config, view)

    assert consumer._try_dequeue() is None and consumer._claim_deferred


def test_a_started_retry_is_not_held_by_a_later_unstarted_turn_and_holds_it(
    conn, consumer, int_db_config, view,
):
    """Precedence, not a deadlock: a started retry goes before an unstarted turn that
    came due while it backed off, even when that turn has the lower id."""
    follow_up = _publish(int_db_config, view)
    _sql(conn, "UPDATE job SET scheduled_after = NOW() + INTERVAL 1 HOUR WHERE id = %s", (follow_up,))
    retry = _publish(int_db_config, view)
    assert consumer._try_dequeue().id == retry
    _sql(conn, "UPDATE job SET status = 'TODO', scheduled_after = NOW() + INTERVAL 1 HOUR "
               "WHERE id = %s", (retry,))
    _sql(conn, "UPDATE job SET scheduled_after = NOW() WHERE id = %s", (follow_up,))

    assert consumer._try_dequeue() is None                     # the follow-up waits
    _sql(conn, "UPDATE job SET scheduled_after = NOW() WHERE id = %s", (retry,))
    assert consumer._try_dequeue().id == retry


@pytest.mark.parametrize(("session", "held"), [("s-paused", True), (None, False)])
def test_a_paused_turn_holds_its_line_only_while_it_can_resume(
    conn, consumer, int_db_config, view, session, held,
):
    paused = _publish(int_db_config, view)
    assert consumer._try_dequeue().id == paused
    _sql(conn, "UPDATE job SET status = 'PAUSED', session_id = %s WHERE id = %s", (session, paused))
    later = _publish(int_db_config, view)

    job = consumer._try_dequeue()

    assert (job is None) is held
    if not held:
        assert job.id == later


def test_a_child_line_is_not_blocked_by_its_running_root_turn(conn, consumer, int_db_config, view):
    root = _publish(int_db_config, view)
    assert consumer._try_dequeue().id == root
    thread_id = _row(conn, root)["thread_id"]
    child = _publish(int_db_config, view, reference_id=str(root), thread_id=thread_id,
                     parent_job_id=root, depth=1)

    assert consumer._try_dequeue().id == child


def test_a_chain_of_turns_is_linear(conn, consumer, int_db_config, view):
    claimed: list[int] = []
    for _ in range(5):
        job_id = _publish(int_db_config, view)
        job = consumer._try_dequeue()
        assert job.id == job_id
        claimed.append(job_id)
        assert _row(conn, job_id)["resume_job_id"] == (claimed[-2] if len(claimed) > 1 else None)
        assert job.resume_job_id == _row(conn, job_id)["resume_job_id"]
        _finish(conn, job_id, session_id=f"s-{job_id}")


def test_a_turn_with_no_earlier_session_starts_fresh(conn, consumer, int_db_config, view):
    first = _publish(int_db_config, view)
    assert consumer._try_dequeue().id == first
    _finish(conn, first, session_id=None)
    second = _publish(int_db_config, view)

    assert consumer._try_dequeue().id == second
    assert _row(conn, second)["resume_job_id"] is None


def test_a_depth_one_line_follows_its_stored_line_past_fifty_turns(conn, consumer, int_db_config, view):
    root = _publish(int_db_config, view)
    thread_id = _row(conn, root)["thread_id"]
    child = dict(reference_id=str(root), thread_id=thread_id, parent_job_id=root, depth=1)
    start = _publish(int_db_config, view, **child)
    turns = [_publish(int_db_config, view, line_id=start, **child) for _ in range(55)]
    _sql(conn, "UPDATE job SET status = 'SUCCESS', session_id = CONCAT('s-', id) WHERE id <> %s", (root,))
    # A sibling line in the same thread and a later root turn are other lines.
    sibling = _publish(int_db_config, view, **child)
    _finish(conn, sibling)
    later = _publish(int_db_config, view, line_id=start, **child)
    _sql(conn, "UPDATE job SET status = 'SUCCESS' WHERE id = %s", (root,))

    job = consumer._try_dequeue()
    assert job.id == later
    assert _row(conn, later)["resume_job_id"] == turns[-1]
    assert _row(conn, later)["line_id"] == start


def test_a_detach_moves_the_job_to_its_default_thread_before_resume(
    conn, consumer, int_db_config, view, detach_when,
):
    first = _publish(int_db_config, view, thread="mail:conv-1")
    assert consumer._try_dequeue().id == first
    _finish(conn, first, session_id="mail-thread-session")
    second = _publish(int_db_config, view, thread="mail:conv-1")
    shared = _row(conn, second)["thread_id"]
    detach_when.add(second)

    job = consumer._try_dequeue()

    assert job.id == second and job.thread_detached and job.resume_job_id is None
    row = _row(conn, second)
    assert row["thread_id"] != shared and row["resume_job_id"] is None
    [own] = _sql(conn, "SELECT ref FROM thread WHERE id = %s", (row["thread_id"],))
    assert own["ref"] == "jira:AI-1"


def test_a_detach_of_a_job_in_its_default_thread_keeps_it_there(
    conn, consumer, int_db_config, view, detach_when,
):
    first = _publish(int_db_config, view)
    assert consumer._try_dequeue().id == first
    _finish(conn, first)
    second = _publish(int_db_config, view)
    detach_when.add(second)

    job = consumer._try_dequeue()

    assert (job.id, job.thread_detached, job.resume_job_id) == (second, False, first)


def test_a_revocation_between_attempts_detaches_attempt_two(
    conn, consumer, int_db_config, view, detach_when,
):
    first = _publish(int_db_config, view, thread="mail:conv-1")
    assert consumer._try_dequeue().id == first
    _finish(conn, first, session_id="earlier-turn")
    second = _publish(int_db_config, view, thread="mail:conv-1")
    assert consumer._try_dequeue().resume_job_id == first      # attempt 1 continues the thread
    # Attempt 1 fails and is retried, with its own session on the row.
    _sql(conn, "UPDATE job SET status = 'TODO', session_id = 'attempt-1', scheduled_after = NOW() "
               "WHERE id = %s", (second,))
    detach_when.add(second)                                    # the operator turns the gate off

    job = consumer._try_dequeue()

    assert (job.id, job.attempt, job.session_id, job.resume_job_id) == (second, 2, None, None)
    row = _row(conn, second)
    assert row["session_id"] is None and row["thread_id"] != _row(conn, first)["thread_id"]


@pytest.mark.parametrize("by", ["ref", "thread_id"])
def test_the_jobs_of_one_thread_commit_in_id_order(conn, int_db_config, view, by):
    """`line_blocked` reads "an earlier id is open": a later job must not commit (and be
    claimed) while an earlier one of its thread is still uncommitted."""
    from agento.framework.publisher import insert_job

    thread_id = _row(conn, _publish(int_db_config, view))["thread_id"]
    where = {"thread": "jira:AI-1"} if by == "ref" else {"thread_id": thread_id}

    def insert(c, key) -> int:
        with c.cursor() as cur:
            return insert_job(cur, agent_type=AgentType.TODO, source="jira", agent_view_id=view,
                              priority=50, reference_id="AI-1", idempotency_key=key,
                              max_attempts=1, requester=None, **where)

    first = _test_connection()
    first_id = insert(first, "order:1")                       # not committed yet
    second: list[int] = []
    later = threading.Thread(target=lambda: second.append(insert(c := _test_connection(), "order:2"))
                             or c.commit() or c.close())
    later.start()
    later.join(timeout=0.5)
    assert later.is_alive() and second == []                  # it waits for the thread row
    first.commit()
    first.close()
    later.join(timeout=10)
    assert second and second[0] > first_id


def test_the_claims_of_one_thread_run_one_at_a_time(conn, consumer, int_db_config, view):
    """The claim locks its thread row before the line rules read the line, so two consumers
    never both read the other's turn before it changes."""
    job_id = _publish(int_db_config, view)
    thread_id = _row(conn, job_id)["thread_id"]
    holder = _test_connection(autocommit=False)
    with holder.cursor() as cur:
        cur.execute("SELECT id FROM thread WHERE id = %s FOR UPDATE", (thread_id,))
    claimed: list = []
    claim = threading.Thread(target=lambda: claimed.append(consumer._try_dequeue()))
    claim.start()
    claim.join(timeout=0.5)
    assert claim.is_alive() and claimed == []                 # it waits for the thread row
    holder.commit()
    holder.close()
    claim.join(timeout=10)
    assert claimed and claimed[0] is not None and claimed[0].id == job_id


def test_a_job_cannot_join_another_views_thread(conn, int_db_config, view):
    thread_id = _row(conn, _publish(int_db_config, view))["thread_id"]

    with pytest.raises(ValueError):
        _publish(int_db_config, None, thread_id=thread_id)
    assert _sql(conn, "SELECT COUNT(*) AS n FROM job")[0]["n"] == 1


def test_a_sub_agent_of_another_view_joins_its_parents_thread(conn, consumer, int_db_config, view):
    """A child in another view copies its parent's thread (plan §4.1), and resumes no session
    of the parent's view (SEC-7); at depth 0 the same join is refused."""
    parent = _publish(int_db_config, view)
    thread_id = _row(conn, parent)["thread_id"]
    assert consumer._try_dequeue().id == parent
    _finish(conn, parent, session_id="parent-view-session")
    other = _sql(conn, "SELECT workspace_id FROM agent_view WHERE id = %s", (view,))[0]["workspace_id"]
    _sql(conn, "INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, 't_av2', 't')",
         (other,))
    other_view = _sql(conn, "SELECT id FROM agent_view WHERE code = 't_av2'")[0]["id"]

    with pytest.raises(ValueError):
        _publish(int_db_config, other_view, thread_id=thread_id)
    child = _publish(int_db_config, other_view, thread_id=thread_id, parent_job_id=parent, depth=1)

    job = consumer._try_dequeue()
    assert job.id == child and job.resume_job_id is None
    row = _row(conn, child)
    assert row["thread_id"] == thread_id and row["resume_job_id"] is None


def test_a_reference_of_512_characters_has_a_thread(conn, int_db_config, view):
    job_id = _publish(int_db_config, view, reference_id="x" * 512)
    [thread] = _sql(conn, "SELECT ref FROM thread WHERE id = %s", (_row(conn, job_id)["thread_id"],))
    assert thread["ref"] == "jira:" + "x" * 512


def test_a_row_inserted_with_no_thread_gets_its_default_one_at_claim(conn, consumer, view):
    _sql(conn, "INSERT INTO job (type, source, agent_view_id, reference_id, idempotency_key, status) "
               "VALUES ('todo', 'jira', %s, 'AI-9', 'raw:1', 'TODO')", (view,))

    job = consumer._try_dequeue()

    [thread] = _sql(conn, "SELECT ref, agent_view_id FROM thread WHERE id = %s", (job.thread_id,))
    assert thread == {"ref": "jira:AI-9", "agent_view_id": view}
    assert _row(conn, job.id)["thread_id"] == job.thread_id


def test_the_upgrade_patch_gives_every_open_legacy_job_its_default_thread(conn, int_db_config, view):
    existing = _publish(int_db_config, view, reference_id="AI-5")
    for i, status in enumerate(("TODO", "RUNNING", "PAUSED", "SUCCESS", "DEAD")):
        _sql(conn, "INSERT INTO job (type, source, agent_view_id, reference_id, idempotency_key, status) "
                   "VALUES ('todo', 'jira', %s, 'AI-5', %s, %s)", (view, f"legacy:{i}", status))

    with _test_connection() as patch_conn:
        AssignOpenJobThreads().apply(patch_conn)
        AssignOpenJobThreads().apply(patch_conn)              # a rerun is a no-op

    rows = _sql(conn, "SELECT status, thread_id FROM job WHERE idempotency_key LIKE 'legacy:%%'")
    shared = _row(conn, existing)["thread_id"]
    assert {r["status"]: r["thread_id"] for r in rows} == {
        "TODO": shared, "RUNNING": shared, "PAUSED": shared, "SUCCESS": None, "DEAD": None}
    assert _sql(conn, "SELECT COUNT(*) AS n FROM thread")[0]["n"] == 1
