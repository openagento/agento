"""The claim's thread steps and the session a run continues (framework/threads.py, consumer)."""
from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.consumer import Consumer, _session_missing, _should_resume
from agento.framework.consumer_config import ConsumerConfig
from agento.framework.database_config import DatabaseConfig
from agento.framework.defer import defer_seconds
from agento.framework.event_manager import ObserverEntry, get_event_manager
from agento.framework.event_manager import clear as clear_event_manager
from agento.framework.events import ClaimVerdict
from agento.framework.execution_hooks import clear as clear_hooks
from agento.framework.execution_hooks import register_resume_session_resolver
from agento.framework.job_models import AgentType, Job
from agento.framework.threads import LINE_DEFER_MAX_MS, LINE_DEFER_MS, line_defer_ms

_NOW = datetime(2026, 1, 1)
_ROW = {
    "id": 10, "schedule_id": None, "type": "todo", "source": "jira", "reference_id": "AI-1",
    "idempotency_key": "k", "status": "TODO", "attempt": 0, "max_attempts": 3,
    "scheduled_after": _NOW, "started_at": None, "finished_at": None, "result_summary": None,
    "error_message": None, "error_class": None, "created_at": _NOW, "updated_at": _NOW,
    "thread_id": 5, "depth": 0,
}


@pytest.fixture(autouse=True)
def _clean():
    clear_event_manager()
    clear_hooks()
    yield
    clear_event_manager()
    clear_hooks()


def _claim(*, blocked=False, target=None, observer=None, defer_count=1):
    """Run one `_try_dequeue` with the thread steps recorded in call order."""
    order: list[str] = []
    cur = MagicMock()
    cur.fetchone.return_value = dict(_ROW)
    cur.execute.side_effect = lambda sql, *a: order.append(sql.split()[0])
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if observer is not None:
        get_event_manager().register("job_claim_before", ObserverEntry(name="o", observer_class=observer))

    def place(_cur, job, *, detach):
        order.append(f"place(detach={detach})")

    def lock(_cur, thread_id):
        order.append(f"lock_thread({thread_id})")
        return "ab" * 16

    def line(_cur, job):
        order.append("line_blocked")
        return blocked

    def resume(_cur, job):
        order.append("resolve_resume")
        return target

    consumer = Consumer(DatabaseConfig(), ConsumerConfig(), logging.getLogger("test"))
    with (patch("agento.framework.consumer.get_connection", return_value=conn),
          patch("agento.framework.consumer.place_claim", side_effect=place),
          patch("agento.framework.consumer.lock_thread", side_effect=lock),
          patch("agento.framework.consumer.line_blocked", side_effect=line),
          patch("agento.framework.consumer.resolve_resume", side_effect=resume),
          patch("agento.framework.consumer.open_or_bump_stretch", return_value=(1, defer_count)) as bump,
          patch("agento.framework.consumer.write_outbox"),
          patch("agento.framework.consumer.close_stretch", return_value=None)):
        job = consumer._try_dequeue()
    return job, cur, order, bump


class Detaching:
    def execute(self, event) -> None:
        event.detach_thread = True


class Fresh:
    def execute(self, event) -> None:
        event.fresh_session = True


class Deferring:
    def execute(self, event) -> None:
        event.verdict = ClaimVerdict.DEFER
        event.delay_ms = 5000


def test_the_thread_is_placed_and_locked_before_the_line_rules_and_the_target_goes_into_the_claim():
    job, cur, order, _bump = _claim(target=9, observer=Detaching)

    steps = [s for s in order if s not in ("SET", "SELECT")]
    assert steps[:4] == ["place(detach=True)", "lock_thread(5)", "line_blocked", "resolve_resume"]
    claim = next(c.args for c in cur.execute.call_args_list if "status = 'RUNNING'" in c.args[0])
    assert "resume_job_id = %s" in claim[0] and claim[1] == (9, 10)
    assert job is not None and job.resume_job_id == 9
    assert job.thread_key == "ab" * 16  # the lock's read names the thread's dir


def test_the_claim_event_asks_for_no_fresh_session_by_default():
    from agento.framework.events import JobClaimBeforeEvent

    assert JobClaimBeforeEvent(job_id=1).fresh_session is False


def test_a_fresh_session_keeps_the_thread_and_line_and_writes_no_resume_target():
    job, cur, order, _bump = _claim(target=9, observer=Fresh)

    steps = [s for s in order if s not in ("SET", "SELECT")]
    assert steps[:3] == ["place(detach=False)", "lock_thread(5)", "line_blocked"]
    assert "resolve_resume" not in order
    claim = next(c.args for c in cur.execute.call_args_list if "status = 'RUNNING'" in c.args[0])
    assert claim[1] == (None, 10)
    assert job is not None and (job.resume_job_id, job.fresh_session, job.thread_id) == (None, True, 5)


@pytest.mark.parametrize("defer_count", [1, 3, 20])
def test_an_open_earlier_turn_of_the_line_defers_through_the_existing_path(defer_count):
    job, cur, order, bump = _claim(blocked=True, defer_count=defer_count)

    assert job is None
    assert "resolve_resume" not in order
    update = next(c.args for c in cur.execute.call_args_list if c.args[0].startswith("UPDATE job SET scheduled_after"))
    assert update[1] == (defer_seconds(line_defer_ms(defer_count)), 10)
    bump.assert_called_once_with(cur, job_id=10, line_wait=True)   # it waits on its line
    assert not any("status = 'RUNNING'" in c.args[0] for c in cur.execute.call_args_list)


def test_an_observer_deferral_keeps_its_own_delay_and_skips_the_line_query():
    job, cur, order, bump = _claim(observer=Deferring)
    bump.assert_called_once_with(cur, job_id=10, line_wait=False)  # another rule, not the line

    assert job is None and "line_blocked" not in order
    update = next(c.args for c in cur.execute.call_args_list if c.args[0].startswith("UPDATE job SET scheduled_after"))
    assert update[1] == (5, 10)


@pytest.mark.parametrize(("count", "ms"), [
    (1, LINE_DEFER_MS), (2, 2 * LINE_DEFER_MS), (3, 4 * LINE_DEFER_MS), (4, LINE_DEFER_MAX_MS),
    (10**6, LINE_DEFER_MAX_MS),
])
def test_a_waiting_turn_backs_off_up_to_the_cap(count, ms):
    assert line_defer_ms(count) == ms


def test_the_deferral_flag_says_why_the_claim_returned_none():
    assert _claim(blocked=True)[0] is None
    consumer = Consumer(DatabaseConfig(), ConsumerConfig(), logging.getLogger("test"))
    cur = MagicMock()
    cur.fetchone.return_value = None              # an empty queue
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    consumer._claim_deferred = True
    with patch("agento.framework.consumer.get_connection", return_value=conn):
        assert consumer._try_dequeue() is None
    assert consumer._claim_deferred is False


@pytest.mark.parametrize(("deferred", "sleeps"), [(True, 0), (False, 1)])
def test_the_loop_sleeps_after_an_empty_queue_but_not_after_a_deferral(deferred, sleeps):
    """A deferred job moved back by its delay; the next pass must reach the jobs behind it
    at once, or one waiting turn holds the head of the queue for every source."""
    consumer = Consumer(MagicMock(), ConsumerConfig(poll_interval=5), logging.getLogger("t"))
    waits: list[float] = []
    passes = [0]

    def dequeue():
        passes[0] += 1
        consumer._claim_deferred = deferred and passes[0] == 1
        if passes[0] == 2:
            consumer._shutdown.set()
        return None

    real_wait = consumer._shutdown.wait

    def wait(timeout=None):
        waits.append(timeout)
        return real_wait(0)

    with (patch.object(consumer, "_recover_stale_jobs"),
          patch.object(consumer, "_maybe_reload_bootstrap"),
          patch.object(consumer, "_renew_leases"),
          patch.object(consumer, "_try_dequeue", side_effect=dequeue),
          patch.object(consumer._shutdown, "wait", side_effect=wait),
          patch("agento.framework.consumer.get_event_manager"),
          patch("agento.framework.consumer.dispatch_shutdown"),
          patch("signal.signal")):
        consumer.run()

    # Pass 2 is the empty queue in both cases; only pass 1 differs.
    assert len([w for w in waits if w == 5]) == 1 + sleeps


class TestResumeSession:
    @staticmethod
    def _conn(row):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = row
        return conn

    @staticmethod
    def _job(**fields) -> Job:
        job = Job.stub(type=AgentType.TODO, source="jira", reference_id="AI-1")
        job.id, job.attempt = 10, 1
        for name, value in fields.items():
            setattr(job, name, value)
        return job

    def test_the_target_session_of_the_same_harness_is_resumed(self):
        conn = self._conn({"session_id": "s-9", "agent_type": "h1"})
        assert Consumer._resume_session(conn, self._job(resume_job_id=9), "h1") == "s-9"

    @pytest.mark.parametrize(("row", "attempt"), [
        ({"session_id": "s-9", "agent_type": "h2"}, 1),   # another harness made it
        (None, 1),                                       # the target row is gone
    ])
    def test_otherwise_the_run_is_fresh(self, row, attempt):
        job = self._job(resume_job_id=9, attempt=attempt)
        assert Consumer._resume_session(self._conn(row), job, "h1") is None

    def test_a_retry_with_a_session_of_its_own_does_not_take_the_target(self):
        conn = self._conn({"session_id": "s-9", "agent_type": "h1"})
        job = self._job(resume_job_id=9, attempt=2, session_id="own")
        assert Consumer._resume_session(conn, job, "h1") is None

    def test_a_retry_whose_first_attempt_saved_no_session_continues_the_line(self):
        conn = self._conn({"session_id": "s-9", "agent_type": "h1"})
        job = self._job(resume_job_id=9, attempt=2, session_id=None)
        assert Consumer._resume_session(conn, job, "h1") == "s-9"

    def test_a_fresh_session_retry_still_resumes_its_own_crashed_attempt(self):
        # The retry path (`_should_resume`) reads the job's own session, not the target.
        job = self._job(attempt=2, session_id="own", fresh_session=True)
        assert Consumer._resume_session(MagicMock(), job, "h1") is None
        assert _should_resume(attempt=2, session_id=job.session_id, pid_alive=False,
                              can_resume=True)

    @pytest.mark.parametrize(("detached", "fresh", "expected", "calls"), [
        (False, False, "legacy", 1), (True, False, None, 0), (False, True, None, 0)])
    def test_with_no_target_only_a_job_this_claim_did_not_detach_asks_the_deprecated_resolver(
        self, detached, fresh, expected, calls,
    ):
        class Resolver:
            calls = 0

            def resolve(self, *, conn, job_id: int, attempt: int) -> str | None:
                Resolver.calls += 1
                return "legacy"

        register_resume_session_resolver(Resolver(), module="m")
        job = self._job(thread_detached=detached, fresh_session=fresh)
        assert Consumer._resume_session(MagicMock(), job, "h1") == expected
        assert Resolver.calls == calls

    @staticmethod
    def _entry(found):
        def session_file(root, session_id):
            if found == "raise":
                raise OSError("boom")
            return root / f"{session_id}.jsonl" if found else None
        return SimpleNamespace(adapter=SimpleNamespace(session_file=session_file))

    @pytest.mark.parametrize(("entry", "home", "expected"), [
        ("present", "/h", "s-9"),
        ("missing", "/h", None),
        ("no member", "/h", "s-9"),   # a harness without `session_file`: today's result
        ("raises", "/h", "s-9"),
        ("missing", None, "s-9"),     # no run HOME to look in
    ])
    def test_a_target_session_with_no_file_gives_a_fresh_session(self, entry, home, expected, caplog):
        entries = {"present": self._entry(True), "missing": self._entry(False),
                   "no member": SimpleNamespace(adapter=object()), "raises": self._entry("raise")}
        conn = self._conn({"session_id": "s-9", "agent_type": "h1"})
        with caplog.at_level(logging.INFO):
            got = Consumer._resume_session(conn, self._job(resume_job_id=9), "h1",
                                           harness_entry=entries[entry], home=home)
        assert got == expected
        logged = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert logged == (["Job 10: session s-9 has no file, starting a fresh session"]
                          if expected is None else [])

    def test_the_deprecated_resolver_session_with_no_file_gives_a_fresh_session(self):
        class Resolver:
            def resolve(self, *, conn, job_id: int, attempt: int) -> str | None:
                return "legacy"

        register_resume_session_resolver(Resolver(), module="m")
        assert Consumer._resume_session(MagicMock(), self._job(), "h1",
                                        harness_entry=self._entry(False), home="/h") is None

    @pytest.mark.parametrize(("found", "resumes"), [(True, True), (False, False)])
    def test_a_retry_resumes_its_own_session_only_when_its_file_is_there(self, found, resumes):
        present = not _session_missing(self._entry(found), "/h", "own")
        assert _should_resume(attempt=2, session_id="own", pid_alive=False, can_resume=True,
                              session_present=present) is resumes

