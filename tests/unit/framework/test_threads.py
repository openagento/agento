"""framework/threads.py: identity, paths, lines and the claim placement, on a fake cursor.

The SQL itself is proven against MySQL in tests/integration/test_thread_claim.py.
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from agento.framework import threads
from agento.framework.job_models import AgentType, Job


def _cursor(*fetchone, lastrowid=77):
    cur = MagicMock()
    cur.fetchone.side_effect = list(fetchone)
    cur.lastrowid = lastrowid
    return cur


def _job(**fields) -> Job:
    job = Job.stub(type=AgentType.TODO, source="jira", reference_id="AI-1", agent_view_id=3)
    job.id = 10
    for name, value in fields.items():
        setattr(job, name, value)
    return job


@pytest.mark.parametrize(("reference_id", "expected"), [
    ("AI-1", "jira:AI-1"), (None, "job:10"), ("", "job:10"),
])
def test_default_ref(reference_id, expected):
    assert threads.default_ref("jira", reference_id, 10) == expected


class TestEnsureThread:
    def test_an_existing_thread_is_found_without_an_insert(self):
        cur = _cursor({"id": 5})
        assert threads.ensure_thread(cur, agent_view_id=3, ref="jira:AI-1") == 5
        [(sql, params)] = [c.args for c in cur.execute.call_args_list]
        assert sql.startswith("SELECT") and params == (3, hashlib.sha1(b"jira:AI-1").hexdigest())

    def test_a_missing_thread_is_upserted_with_a_random_key(self):
        cur = _cursor(None)
        assert threads.ensure_thread(cur, agent_view_id=3, ref="jira:AI-1") == 77
        sql, params = cur.execute.call_args.args
        assert "ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)" in sql
        assert params[:3] == (3, "jira:AI-1", hashlib.sha1(b"jira:AI-1").hexdigest())
        assert len(params[3]) == 32

    def test_with_no_view_every_call_is_a_new_thread(self):
        # NULL is not equal to NULL in a unique key: there is nothing to look up.
        cur = _cursor()
        threads.ensure_thread(cur, agent_view_id=None, ref="jira:AI-1")
        [(sql, _params)] = [c.args for c in cur.execute.call_args_list]
        assert sql.startswith("INSERT INTO thread")

    def test_a_ref_of_a_512_character_reference_fits(self):
        threads.ensure_thread(_cursor(None), agent_view_id=3, ref="x" * 64 + ":" + "r" * 512)

    def test_a_ref_longer_than_the_column_is_refused(self):
        with pytest.raises(ValueError):
            threads.ensure_thread(_cursor(), agent_view_id=3, ref="r" * 601)

    def test_lock_locks_an_existing_thread_by_id(self):
        cur = _cursor({"id": 5})
        assert threads.ensure_thread(cur, agent_view_id=3, ref="jira:AI-1", lock=True) == 5
        assert cur.execute.call_args.args == ("SELECT id FROM thread WHERE id = %s FOR UPDATE", (5,))


class TestJoinThread:
    def test_a_thread_of_the_jobs_view_is_locked(self):
        cur = _cursor({"agent_view_id": 3})
        threads.join_thread(cur, 9, agent_view_id=3, depth=0)
        sql, params = cur.execute.call_args.args
        assert sql.endswith("FOR UPDATE") and params == (9,)

    def test_a_job_with_no_view_joins_no_thread(self):
        cur = _cursor({"agent_view_id": None})
        with pytest.raises(ValueError):
            threads.join_thread(cur, 9, agent_view_id=None, depth=0)
        cur.execute.assert_not_called()

    @pytest.mark.parametrize("row", [None, {"agent_view_id": 4}, {"agent_view_id": None}])
    def test_a_missing_thread_or_another_views_thread_is_refused(self, row):
        with pytest.raises(ValueError):
            threads.join_thread(_cursor(row), 9, agent_view_id=3, depth=0)

    @pytest.mark.parametrize("row", [{"agent_view_id": 4}, {"agent_view_id": None}])
    def test_a_sub_agent_joins_its_parents_thread_of_another_view(self, row):
        threads.join_thread(_cursor(row), 9, agent_view_id=3, depth=1)

    def test_a_sub_agent_cannot_join_a_missing_thread(self):
        with pytest.raises(ValueError):
            threads.join_thread(_cursor(None), 9, agent_view_id=3, depth=1)


@pytest.mark.parametrize(("depth", "line_id", "expected"), [(0, None, None), (1, None, 10), (2, 4, 4)])
def test_line_key(depth, line_id, expected):
    assert threads.line_key(_job(depth=depth, line_id=line_id)) == expected


class TestPlaceClaim:
    def test_a_job_in_a_thread_is_left_alone(self):
        cur = _cursor()
        threads.place_claim(cur, _job(thread_id=5), detach=False)
        cur.execute.assert_not_called()

    def test_a_job_with_no_thread_gets_its_default_one(self):
        job = _job(thread_id=None, session_id="s")
        cur = _cursor({"id": 8})
        threads.place_claim(cur, job, detach=False)
        assert (job.thread_id, job.session_id, job.thread_detached) == (8, "s", False)
        assert cur.execute.call_args.args == ("UPDATE job SET thread_id = %s WHERE id = %s", (8, 10))

    def test_a_detach_moves_the_job_and_clears_its_session_in_one_statement(self):
        job = _job(thread_id=5, session_id="old-session")
        cur = _cursor({"id": 8})
        threads.place_claim(cur, job, detach=True)
        assert (job.thread_id, job.session_id, job.thread_detached) == (8, None, True)
        sql, params = cur.execute.call_args.args
        assert "thread_id = %s, session_id = NULL" in sql and params == (8, 10)

    def test_a_detach_of_a_job_already_in_its_own_thread_moves_nothing(self):
        job = _job(thread_id=8, session_id="own-retry-session")
        cur = _cursor({"id": 8})
        threads.place_claim(cur, job, detach=True)
        assert (job.thread_id, job.session_id, job.thread_detached) == (8, "own-retry-session", False)
        assert not any(c.args[0].startswith("UPDATE") for c in cur.execute.call_args_list)

    @pytest.mark.parametrize(("ref", "moved"), [("jira:AI-1", False), ("mail:conv-1", True)])
    def test_with_no_view_a_detach_makes_a_new_thread_only_to_leave_another_one(self, ref, moved):
        job = _job(thread_id=5, agent_view_id=None)
        cur = _cursor({"ref": ref}, lastrowid=9)
        threads.place_claim(cur, job, detach=True)
        assert job.thread_detached is moved
        assert any(c.args[0].startswith("INSERT INTO thread") for c in cur.execute.call_args_list) is moved


@pytest.mark.parametrize(("depth", "line_id", "where", "params"), [
    (0, None, "thread_id = %s AND depth = 0", (5,)),
    (1, None, "(id = %s OR line_id = %s) AND thread_id = %s", (10, 10, 5)),
    (2, 4, "(id = %s OR line_id = %s) AND thread_id = %s", (4, 4, 5)),
])
class TestLineQueries:
    """Both rules read the job's line, and only in its CURRENT thread."""

    @pytest.mark.parametrize(("started", "first"), [
        (True, "started_at IS NOT NULL AND id < %s"),
        (False, "started_at IS NOT NULL OR (id < %s AND (scheduled_after <= NOW() OR EXISTS"),
    ])
    def test_line_blocked(self, depth, line_id, where, params, started, first):
        cur = _cursor({"1": 1})
        job = _job(thread_id=5, depth=depth, line_id=line_id)
        job.started_at = job.started_at if started else None
        assert threads.line_blocked(cur, job)
        sql, args = cur.execute.call_args.args
        # Any RUNNING or PAUSED turn holds the line, whatever its id; a TODO only if first.
        assert where in sql and "id <> %s" in sql and first in sql
        assert "status = 'RUNNING' OR (status = 'PAUSED' AND session_id IS NOT NULL)" in sql
        assert ("s.line_wait = 1" in sql) is not started   # only an unstarted job waits behind one
        assert args == (*params, 10, ("TODO", "RUNNING", "PAUSED"), 10)

    def test_resolve_resume(self, depth, line_id, where, params):
        cur = _cursor({"id": 9})
        assert threads.resolve_resume(cur, _job(thread_id=5, depth=depth, line_id=line_id)) == 9
        sql, args = cur.execute.call_args.args
        assert where in sql and "session_id IS NOT NULL" in sql
        assert "ORDER BY finished_at DESC, id DESC LIMIT 1" in sql and "id <> %s" in sql
        assert args == (*params, 10, ("SUCCESS", "FAILED", "DEAD"))


def test_no_earlier_turn_is_no_block_and_no_target():
    job = _job(thread_id=5)
    assert threads.line_blocked(_cursor(None), job) is False
    assert threads.resolve_resume(_cursor(None), job) is None
