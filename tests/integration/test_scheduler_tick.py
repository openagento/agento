"""The schedule tick and fire() (AG-67 plan §5.3), against a real MySQL, with a test-only source."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from agento.framework.event_manager import ObserverEntry, get_event_manager
from agento.framework.schedule import sources, tick
from agento.framework.schedule.sources import FireSpec
from agento.framework.schedule.tick import FireOutcome, fire

from .conftest import _test_connection

NOW = datetime(2026, 6, 1, 12, 0)
HOURLY = "FREQ=HOURLY;BYMINUTE=0"


class _Source:
    name = "testsrc"
    editable = True

    def fire_spec(self, cur, row):
        return FireSpec("testsrc", "blank", str(row["id"]), f"schedule:{row['id']}", None, row["prompt"], 50)

    def on_delete(self, cur, row):
        cur.execute("INSERT INTO schedule_test_log (schedule_id) VALUES (%s)", (row["id"],))


@pytest.fixture
def conn():
    c = _test_connection()
    with c.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS schedule_test_log (schedule_id BIGINT UNSIGNED NOT NULL)")
        cur.execute("DELETE FROM schedule_test_log")
    c.commit()
    yield c
    c.rollback()
    c.close()


@pytest.fixture
def registered(monkeypatch):
    found = {"testsrc": _Source()}
    monkeypatch.setattr(sources, "load_sources", lambda: dict(found))
    return found


@pytest.fixture
def view(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES ('sch-ws', 'ws')")
        cur.execute("SELECT id FROM workspace WHERE code = 'sch-ws'")
        ws = cur.fetchone()["id"]
        cur.execute("INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, 'sch-av', 'av')", (ws,))
        cur.execute("SELECT id FROM agent_view WHERE code = 'sch-av'")
        av = cur.fetchone()["id"]
        cur.execute("UPDATE workspace SET is_active = 1 WHERE id = %s", (ws,))
        cur.execute("UPDATE agent_view SET is_active = 1 WHERE id = %s", (av,))
    conn.commit()
    return av


class _Recorder:
    seen: list = []  # noqa: RUF012
    veto = False
    fail = False

    def execute(self, event):
        _Recorder.seen.append(event)
        if _Recorder.fail and type(event).__name__ == "ScheduleRunBeforeEvent":
            raise RuntimeError("observer broke")
        if _Recorder.veto and hasattr(event, "veto"):
            event.veto = True


@pytest.fixture
def events():
    _Recorder.seen, _Recorder.veto, _Recorder.fail = [], False, False
    em = get_event_manager()
    names = ("schedule_run_before", "schedule_run_after", "job_publish_after", "schedule_delete_after")
    for name in names:
        em.register(name, ObserverEntry(name=f"test_{name}", observer_class=_Recorder))
    yield _Recorder
    for name in names:
        em._observers[name] = [e for e in em._observers[name] if e.observer_class is not _Recorder]


def _names(events) -> list[str]:
    return [type(e).__name__ for e in events.seen]


def _schedule(conn, view, **over) -> int:
    row = dict(source="testsrc", title="t", agent_view_id=view, owner_agent_view_id=view, prompt="do it",
               rrule=HOURLY, starts_at=datetime(2026, 6, 1), timezone="UTC", enabled=1,
               next_fire_at=NOW)
    row.update(over)
    cols = ", ".join(row)
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO schedule ({cols}) VALUES ({', '.join(['%s'] * len(row))})", tuple(row.values()))
        sid = cur.lastrowid
    conn.commit()
    return sid


def _row(conn, sid) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM schedule WHERE id = %s", (sid,))
        row = cur.fetchone()
    conn.rollback()
    return row


def _jobs(conn, sid=None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM job" + ("" if sid is None else " WHERE schedule_id = %s") + " ORDER BY id",
                    () if sid is None else (sid,))
        rows = list(cur.fetchall())
    conn.rollback()
    return rows


def _tick(conn, now=NOW, batch=500):
    return tick.tick(conn, now=now, batch=batch)


class TestFireDue:
    def test_publishes_one_job_and_advances(self, conn, view, registered, events):
        sid = _schedule(conn, view)
        result = _tick(conn)
        assert (result.published, result.skipped) == (1, 0)
        [job] = _jobs(conn, sid)
        assert (job["source"], job["type"], job["reference_id"], job["context"]) == ("testsrc", "blank", str(sid),
                                                                                     "do it")
        assert job["idempotency_key"] == f"schedule:{sid}:20260601T1200"
        row = _row(conn, sid)
        assert row["last_fire_at"] == datetime(2026, 6, 1, 12, 0)
        assert row["next_fire_at"] == datetime(2026, 6, 1, 13, 0)
        assert row["anchor_local"] == datetime(2026, 6, 1, 12, 0)
        assert _names(events) == ["ScheduleRunBeforeEvent", "JobPublishedEvent", "ScheduleRunEvent"]
        assert events.seen[2].job_id == job["id"] and events.seen[2].schedule_id == sid

    def test_harness_and_model_are_pinned_on_the_job(self, conn, view, registered):
        sid = _schedule(conn, view, harness="h1", model="m1")
        _tick(conn)
        [job] = _jobs(conn, sid)
        assert (job["agent_type"], job["model"]) == ("h1", "m1")

    def test_a_missed_fire_is_skipped_not_caught_up(self, conn, view, registered):
        sid = _schedule(conn, view, next_fire_at=NOW - timedelta(hours=5))
        _tick(conn)
        assert len(_jobs(conn, sid)) == 1  # one late fire, not five
        assert _row(conn, sid)["next_fire_at"] == datetime(2026, 6, 1, 13, 0)

    def test_the_same_key_publishes_once(self, conn, view, registered):
        sid = _schedule(conn, view, next_fire_at=None)
        for _ in range(2):
            with conn.cursor() as cur:
                cur.execute("UPDATE schedule SET last_fire_at = NULL WHERE id = %s", (sid,))
                fire(cur, sid, fire_at=NOW, idempotency_key="k1", actor="test", min_interval=5)
            conn.commit()
        assert len(_jobs(conn, sid)) == 1

    def test_count_end_disables_the_row(self, conn, view, registered):
        sid = _schedule(conn, view, rrule="FREQ=HOURLY;BYMINUTE=0;COUNT=12",
                        next_fire_at=datetime(2026, 6, 1, 11, 0))  # the 12th and last occurrence
        _tick(conn)
        row = _row(conn, sid)
        assert (row["enabled"], row["last_error"], row["next_fire_at"], row["anchor_local"]) == (0, "ended", None, None)
        assert len(_jobs(conn, sid)) == 1

    def test_rows_of_an_unregistered_source_are_not_touched(self, conn, view, registered):
        sid = _schedule(conn, view, source="cron")
        assert _tick(conn).published == 0
        assert _row(conn, sid)["next_fire_at"] == NOW

    def test_a_row_that_raises_is_rolled_back_and_the_loop_goes_on(self, conn, view, registered, monkeypatch):
        bad = _schedule(conn, view, prompt="bad")
        good = _schedule(conn, view)
        real = _Source.fire_spec

        def spec(self, cur, row):
            if row["prompt"] == "bad":
                raise RuntimeError("boom")
            return real(self, cur, row)

        monkeypatch.setattr(_Source, "fire_spec", spec)
        result = _tick(conn)
        assert (result.failed, result.published) == (1, 1)
        assert _jobs(conn, bad) == [] and _row(conn, bad)["last_fire_at"] is None
        assert len(_jobs(conn, good)) == 1


class TestOutcomes:
    def _open_job(self, conn, sid):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO job (type, source, idempotency_key, status, schedule_id) "
                        "VALUES ('blank', 'testsrc', %s, 'RUNNING', %s)", (f"open-{sid}", sid))
        conn.commit()

    def _inactive_owner(self, conn, sid):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO `user` (username, role, is_active) VALUES ('gone', 'admin', 0)")
            cur.execute("UPDATE schedule SET owner_user_id = %s, owner_agent_view_id = NULL WHERE id = %s",
                        (cur.lastrowid, sid))
        conn.commit()

    @pytest.mark.parametrize("make, outcome", [
        ("overlap", FireOutcome.OVERLAP),
        ("too_soon", FireOutcome.TOO_SOON),
        ("reach", FireOutcome.NO_REACH),
        ("inactive_view", FireOutcome.NO_REACH),
        ("inactive_workspace", FireOutcome.NO_REACH),
        ("veto", FireOutcome.VETOED),
        ("observer_fails", FireOutcome.VETOED),
    ])
    def test_every_outcome_advances_and_publishes_nothing(self, conn, view, registered, events, make, outcome):
        sid = _schedule(conn, view)
        if make == "overlap":
            self._open_job(conn, sid)
        elif make == "too_soon":
            _schedule_update(conn, sid, last_fire_at=NOW - timedelta(minutes=3))
        elif make == "reach":
            self._inactive_owner(conn, sid)
        elif make in ("inactive_view", "inactive_workspace"):
            # A view-owned row (no user owner) still needs an active view and workspace (SEC-9).
            with conn.cursor() as cur:
                if make == "inactive_view":
                    cur.execute("UPDATE agent_view SET is_active = 0 WHERE id = %s", (view,))
                else:
                    cur.execute("UPDATE workspace w JOIN agent_view v ON v.workspace_id = w.id "
                                "SET w.is_active = 0 WHERE v.id = %s", (view,))
            conn.commit()
        elif make == "veto":
            events.veto = True
        else:
            events.fail = True
        with conn.cursor() as cur:
            got, job_id, after = fire(cur, sid, fire_at=NOW, idempotency_key="x",
                                      actor="test", min_interval=5)
        conn.rollback()
        assert (got, job_id, after) == (outcome, None, None)

        result = _tick(conn)
        assert (result.published, result.skipped) == (0, 1)
        row = _row(conn, sid)
        assert row["next_fire_at"] == datetime(2026, 6, 1, 13, 0)
        assert row["anchor_local"] == NOW
        assert [j for j in _jobs(conn, sid) if j["status"] == "TODO"] == []
        dispatched = set(_names(events))
        assert "ScheduleRunEvent" not in dispatched and "JobPublishedEvent" not in dispatched
        if outcome is FireOutcome.VETOED:
            assert "ScheduleRunBeforeEvent" in dispatched
        else:
            assert dispatched == set()

    def test_a_full_batch_of_skipped_rows_does_not_block_runnable_rows(self, conn, view, registered):
        blocked = [_schedule(conn, view, next_fire_at=NOW - timedelta(minutes=10)) for _ in range(3)]
        for sid in blocked:
            self._open_job(conn, sid)
        runnable = _schedule(conn, view)
        assert _tick(conn, batch=3).skipped == 3
        assert _tick(conn, batch=3).published == 1
        assert len(_jobs(conn, runnable)) == 1

    def test_the_skipped_fire_does_not_run_late_when_the_overlap_clears(self, conn, view, registered):
        sid = _schedule(conn, view)
        self._open_job(conn, sid)
        _tick(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE job SET status = 'SUCCESS' WHERE schedule_id = %s", (sid,))
        conn.commit()
        assert _tick(conn, now=NOW + timedelta(minutes=30)).published == 0
        assert _tick(conn, now=NOW + timedelta(hours=1)).published == 1


def _schedule_update(conn, sid, **cols):
    with conn.cursor() as cur:
        cur.execute(f"UPDATE schedule SET {', '.join(f'{k} = %s' for k in cols)} WHERE id = %s",
                    (*cols.values(), sid))
    conn.commit()


class TestFill:
    def test_a_valid_rule_gets_its_next_fire_and_does_not_fire_now(self, conn, view, registered):
        sid = _schedule(conn, view, next_fire_at=None, anchor_local=datetime(2026, 1, 1))
        result = _tick(conn)
        assert (result.filled, result.published) == (1, 0)
        row = _row(conn, sid)
        assert (row["next_fire_at"], row["anchor_local"]) == (datetime(2026, 6, 1, 13, 0), None)

    @pytest.mark.parametrize("rrule, code", [("FREQ=SECONDLY", "too_frequent"), ("nonsense", "bad_rule"),
                                             ("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30", "no_occurrence")])
    def test_an_invalid_rule_disables_the_row(self, conn, view, registered, rrule, code):
        sid = _schedule(conn, view, next_fire_at=None, rrule=rrule)
        assert _tick(conn).disabled == 1
        row = _row(conn, sid)
        assert (row["enabled"], row["last_error"], row["next_fire_at"]) == (0, code, None)

    def test_a_rule_changed_during_validation_is_left_for_the_next_tick(self, conn, view, registered, monkeypatch):
        sid = _schedule(conn, view, next_fire_at=None)
        real = tick.engine.validate

        def edit_meanwhile(*a, **kw):
            other = _test_connection(autocommit=True)
            with other.cursor() as cur:
                cur.execute("UPDATE schedule SET rrule = 'FREQ=DAILY' WHERE id = %s", (sid,))
            other.close()
            return real(*a, **kw)

        monkeypatch.setattr(tick.engine, "validate", edit_meanwhile)
        _tick(conn)
        assert _row(conn, sid)["next_fire_at"] is None


class TestFinalizeDelete:
    def test_phase_a_removes_the_row_calls_on_delete_and_dispatches_after_commit(self, conn, view, registered,
                                                                                events):
        sid = _schedule(conn, view, enabled=0, deleted_at=NOW)
        assert _tick(conn).finalized == 1
        assert _row(conn, sid) is None
        with conn.cursor() as cur:
            cur.execute("SELECT schedule_id FROM schedule_test_log")
            assert [r["schedule_id"] for r in cur.fetchall()] == [sid]
        conn.rollback()
        assert _names(events) == ["ScheduleDeletedEvent"]
        assert events.seen[0].schedule_id == sid and events.seen[0].actor == tick.TICK_ACTOR

    def test_a_rollback_dispatches_nothing(self, conn, view, registered, events, monkeypatch):
        sid = _schedule(conn, view, enabled=0, deleted_at=NOW)
        monkeypatch.setattr(_Source, "on_delete", lambda self, cur, row: (_ for _ in ()).throw(RuntimeError("x")))
        assert _tick(conn).failed == 1
        assert _row(conn, sid) is not None
        assert events.seen == []

    def test_a_disabled_source_keeps_the_row_until_it_is_enabled_again(self, conn, view, registered, events):
        sid = _schedule(conn, view, enabled=0, deleted_at=NOW)
        registered.clear()
        assert _tick(conn).finalized == 0
        assert _row(conn, sid) is not None
        registered["testsrc"] = _Source()
        assert _tick(conn).finalized == 1
        assert _row(conn, sid) is None
        assert _names(events) == ["ScheduleDeletedEvent"]

    def test_a_restored_row_is_not_finalized(self, conn, view, registered):
        sid = _schedule(conn, view)
        with conn.cursor() as cur:
            assert tick.finalize_delete(cur, sid, actor="t") is None
        conn.rollback()
        assert _row(conn, sid) is not None


class TestLocks:
    def test_a_held_tick_lock_makes_the_tick_a_no_op(self, conn, view, registered):
        sid = _schedule(conn, view)
        other = _test_connection(autocommit=True)
        try:
            with other.cursor() as cur:
                cur.execute("SELECT GET_LOCK(CONCAT(%s, DATABASE()), 0)", (tick.TICK_LOCK,))
            assert _tick(conn).lock_held
            assert _jobs(conn, sid) == []
            with other.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK(CONCAT(%s, DATABASE()))", (tick.TICK_LOCK,))
        finally:
            other.close()
        assert not _tick(conn).lock_held

    @pytest.mark.parametrize("second", ["run_now", "tick"])
    def test_run_now_serializes_with_a_second_caller(self, conn, view, registered, second):
        sid = _schedule(conn, view)
        holder = _test_connection()
        outcome: list = []

        def run_second():
            c = _test_connection()
            try:
                if second == "tick":
                    outcome.append(tick.tick(c, now=NOW, batch=10))
                else:
                    with c.cursor() as cur:
                        outcome.append(fire(cur, sid, fire_at=NOW, idempotency_key="run:2", actor="u2",
                                            min_interval=5)[0])
                    c.commit()
            finally:
                c.close()

        try:
            with holder.cursor() as cur:
                assert fire(cur, sid, fire_at=NOW, idempotency_key="run:1", actor="u1",
                            min_interval=5)[0] is FireOutcome.PUBLISHED
            t = threading.Thread(target=run_second)
            t.start()
            t.join(0.5)
            assert t.is_alive()  # waits on the row lock
            holder.commit()
            t.join(10)
        finally:
            holder.close()
        assert len(_jobs(conn, sid)) == 1
        if second == "tick":
            assert (outcome[0].published, outcome[0].skipped) == (0, 1)
        else:
            assert outcome == [FireOutcome.OVERLAP]
