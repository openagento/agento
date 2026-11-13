"""The scheduler module (AG-67 plan §5.5, §7, §8.3, §9): panel routes, store, source and
channel against a real MySQL."""
from __future__ import annotations

from datetime import datetime

import pytest

from agento.framework.access import accounts
from agento.framework.channels.registry import register_channel
from agento.framework.event_manager import ObserverEntry, get_event_manager
from agento.framework.job_types import register_job_type
from agento.framework.schedule import sources, tick
from agento.modules.scheduler.src import routes, store
from agento.modules.scheduler.src.channel import SchedulerChannel
from agento.modules.scheduler.src.source import DELETED_SUFFIX, SchedulerSource

from .conftest import _test_connection
from .test_conversation_submission import _Req

HOURLY = "FREQ=HOURLY;BYMINUTE=0"
LEAVES = ("scheduler.own", "scheduler.own.write", "scheduler.own.share", "scheduler.user", "scheduler.agent")
EVENTS = ("schedule_save_before", "schedule_save_after", "schedule_delete_after", "schedule_share_after",
          "schedule_owner_change_after", "schedule_run_after")


class _Recorder:
    seen: list = []  # noqa: RUF012
    veto = False
    fail = False

    def execute(self, event):
        _Recorder.seen.append(event)
        if type(event).__name__ == "ScheduleSaveBeforeEvent":
            if _Recorder.fail:
                raise RuntimeError("observer broke")
            event.veto = _Recorder.veto


@pytest.fixture
def events():
    _Recorder.seen, _Recorder.veto, _Recorder.fail = [], False, False
    em = get_event_manager()
    for name in EVENTS:
        em.register(name, ObserverEntry(name=f"test_{name}", observer_class=_Recorder))
    yield _Recorder
    for name in EVENTS:
        em._observers[name] = [e for e in em._observers[name] if e.observer_class is not _Recorder]


@pytest.fixture
def registered(monkeypatch):
    """What bootstrap() registers: the source, the channel and the job type."""
    monkeypatch.setattr(sources, "load_sources", lambda: {"scheduler": SchedulerSource()})
    register_job_type("scheduler", module="scheduler")
    register_channel(SchedulerChannel())


@pytest.fixture
def granted(conn, world, registered, events):
    for leaf in LEAVES:
        accounts.add_grant(conn, "user", "operation", leaf)
    return world


def _create(conn, user, **over):
    body = {"title": "Daily report", "prompt": "Write the report.", "agent_view_id": over.pop("view"),
            "rrule": HOURLY, **over}
    return routes.create(_Req(conn, user, json=body))


def _one(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
    conn.rollback()
    return row


def _jira_row(conn, view) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO schedule (source, source_ref, title, agent_view_id, owner_agent_view_id, rrule,"
                    " starts_at, timezone, enabled) VALUES ('jira_periodic', 'AB-1', 'jira', %s, %s, %s,"
                    " '2026-01-01 00:00:00', 'UTC', 1)", (view, view, HOURLY))
        sid = cur.lastrowid
    conn.commit()
    return sid


# --- create, list, limits, veto -----------------------------------------------------------

def test_create_writes_the_row_an_audit_row_and_the_after_event(conn, granted):
    response = _create(conn, granted["owner"], view=granted["view"])
    assert response.status == 201, response.body
    sid = response.body["id"]
    row = _one(conn, "SELECT * FROM schedule WHERE id = %s", (sid,))
    assert (row["source"], row["owner_user_id"], row["rrule"]) == ("scheduler", granted["owner"].id, HOURLY)
    assert row["next_fire_at"] is not None
    assert _one(conn, "SELECT action, actor FROM schedule_audit WHERE schedule_id = %s", (sid,)) == {
        "action": "create", "actor": f"service:web:user:{granted['owner'].id}"}
    assert [type(e).__name__ for e in _Recorder.seen] == ["ScheduleSaveBeforeEvent", "ScheduleSavedEvent"]
    listed = routes.list_schedules(_Req(conn, granted["owner"]))
    assert [r["id"] for r in listed.body["groups"]["mine"]] == [sid]
    assert listed.body["groups"]["mine"][0]["can"]["write"] is True


def test_probe_is_403_without_a_leaf_or_a_share(conn, world, registered):
    assert routes.list_schedules(_Req(conn, world["owner"])).status == 403
    assert _create(conn, world["owner"], view=world["view"]).status == 403


@pytest.mark.parametrize("body,code", [
    ({"rrule": "FREQ=SECONDLY"}, "too_frequent"),
    ({"rrule": "nonsense"}, "bad_rule"),
    ({"timezone": "Mars/Base"}, "bad_timezone"),
    ({"starts_at": "2027-01-01T09:00+02:00"}, "bad_start"),
    ({"harness": "nope"}, "bad_harness"),
    ({"owner_user_id": 1}, "unknown_field"),
    ({"title": "  "}, "bad_title"),
])
def test_create_refuses_a_bad_field(conn, granted, body, code):
    response = _create(conn, granted["owner"], view=granted["view"], **body)
    assert (response.status, response.body["code"]) == (400, code)


def test_create_in_a_view_the_user_does_not_see_is_404(conn, granted):
    assert _create(conn, granted["owner"], view=granted["other_view"]).status == 404


def test_the_per_user_limit_holds_and_admin_is_not_limited(conn, granted):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO core_config_data (path, value) VALUES ('scheduler/limits/max_per_user', '1')")
    conn.commit()
    assert _create(conn, granted["owner"], view=granted["view"]).status == 201
    refused = _create(conn, granted["owner"], view=granted["view"])
    assert (refused.status, refused.body["code"]) == (409, "limit")
    assert _create(conn, granted["admin"], view=granted["view"]).status == 201
    assert _create(conn, granted["admin"], view=granted["view"]).status == 201


@pytest.mark.parametrize("how", ["veto", "fail"])
def test_a_veto_or_a_failed_observer_refuses_the_save(conn, granted, how):
    setattr(_Recorder, how, True)
    response = _create(conn, granted["owner"], view=granted["view"])
    assert (response.status, response.body["code"]) == (409, "vetoed")
    assert _one(conn, "SELECT COUNT(*) AS n FROM schedule")["n"] == 0
    assert "ScheduleSavedEvent" not in [type(e).__name__ for e in _Recorder.seen]


# --- read semantics, read-only source --------------------------------------------------

def test_unreadable_is_404_and_readable_without_the_right_is_403(conn, granted):
    other = _create(conn, granted["admin"], view=granted["other_view"]).body["id"]
    assert routes.get_schedule(_Req(conn, granted["owner"], params={"id": str(other)})).status == 404
    mine = _create(conn, granted["owner"], view=granted["view"]).body["id"]
    # The stranger holds scheduler.user (read) but not scheduler.user.write.
    req = _Req(conn, granted["stranger"], params={"id": str(mine)}, json={"title": "x"})
    assert routes.get_schedule(req).status == 200
    assert routes.update(req).status == 403
    assert routes.delete(req).status == 403
    assert routes.run_now(req).status == 403


def test_a_jira_row_is_read_only_even_for_admin(conn, granted):
    sid = _jira_row(conn, granted["view"])
    for user in (granted["owner"], granted["admin"]):
        req = _Req(conn, user, params={"id": str(sid)}, json={"title": "x"})
        got = routes.get_schedule(req)
        assert got.status == 200 and got.body["can"] == {
            "read": True, "write": False, "share": False, "delete": False, "change_owner": False}
        assert routes.update(req).status == 403
        assert routes.delete(req).status == 403
        assert routes.run_now(req).status == 403
    assert [r["id"] for r in routes.list_schedules(_Req(conn, granted["owner"])).body["groups"]["agent"]] == [sid]


# --- update and timing ------------------------------------------------------------------

def test_a_timing_change_resets_next_fire_and_the_anchor(conn, granted):
    sid = _create(conn, granted["owner"], view=granted["view"]).body["id"]
    with conn.cursor() as cur:
        cur.execute("UPDATE schedule SET anchor_local = '2026-01-01 00:00:00', last_error = 'x' WHERE id = %s",
                    (sid,))
    conn.commit()
    req = _Req(conn, granted["owner"], params={"id": str(sid)}, json={"rrule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"})
    assert routes.update(req).status == 204
    row = _one(conn, "SELECT * FROM schedule WHERE id = %s", (sid,))
    assert (row["anchor_local"], row["last_error"], row["next_fire_at"].hour) == (None, None, 7)


def test_a_title_edit_leaves_the_timing(conn, granted):
    sid = _create(conn, granted["owner"], view=granted["view"]).body["id"]
    before = _one(conn, "SELECT next_fire_at FROM schedule WHERE id = %s", (sid,))
    req = _Req(conn, granted["owner"], params={"id": str(sid)}, json={"title": "New"})
    assert routes.update(req).status == 204
    assert _one(conn, "SELECT title, next_fire_at FROM schedule WHERE id = %s", (sid,)) == {
        "title": "New", **before}


def test_preview_lists_the_next_fires(conn, granted):
    response = routes.preview(_Req(conn, granted["owner"], json={
        "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0", "timezone": "Europe/Warsaw"}))
    assert response.status == 200, response.body
    assert len(response.body["fires"]) >= 1
    assert response.body["fires"][0]["local"].endswith("T09:00:00")
    assert response.body["fires"][0]["at"].endswith("Z")


def test_options_offer_presets_and_target_views(conn, granted):
    body = routes.options(_Req(conn, granted["owner"])).body
    assert {"label": "Every hour", "rrule": HOURLY} in body["presets"]
    assert [v["id"] for v in body["agent_views"]] == [granted["view"]]
    assert body["min_interval_minutes"] >= 1


# --- share, owner change ----------------------------------------------------------------

def test_a_share_gives_read_and_write_and_shows_in_the_shared_group(conn, granted):
    sid = _create(conn, granted["owner"], view=granted["view"]).body["id"]
    put = _Req(conn, granted["owner"], params={"id": str(sid)}, json={"username": "c-stranger"})
    assert routes.put_member(put).status == 204
    assert type(_Recorder.seen[-1]).__name__ == "ScheduleShareEvent"
    stranger = _Req(conn, granted["stranger"], params={"id": str(sid)}, json={"title": "Edited"})
    assert routes.update(stranger).status == 204
    assert routes.put_member(_Req(conn, granted["stranger"], params={"id": str(sid)},
                                  json={"username": "c-admin"})).status == 403
    groups = routes.list_schedules(_Req(conn, granted["stranger"])).body["groups"]
    assert [r["id"] for r in groups["shared"]] == [sid] and groups["user"] == []
    owner_view = routes.get_schedule(_Req(conn, granted["owner"], params={"id": str(sid)})).body
    assert owner_view["members"] == [{"user_id": granted["stranger"].id, "username": "c-stranger"}]
    gone = _Req(conn, granted["owner"], params={"id": str(sid), "user_id": str(granted["stranger"].id)})
    assert routes.delete_member(gone).status == 204
    assert routes.delete_member(gone).status == 404


def test_a_share_alone_passes_the_probe(conn, world, registered, events):
    sid = _create(conn, world["admin"], view=world["view"]).body["id"]
    assert routes.list_schedules(_Req(conn, world["owner"])).status == 403
    routes.put_member(_Req(conn, world["admin"], params={"id": str(sid)}, json={"username": "c-owner"}))
    groups = routes.list_schedules(_Req(conn, world["owner"])).body["groups"]
    assert [r["id"] for r in groups["shared"]] == [sid]
    assert groups["mine"] == groups["agent"] == groups["user"] == []


def test_owner_change_is_admin_only_and_moves_the_conversation(conn, granted):
    sid = _create(conn, granted["owner"], view=granted["view"]).body["id"]
    ran = routes.run_now(_Req(conn, granted["owner"], params={"id": str(sid)}))
    assert ran.status == 201, ran.body
    body = {"user_id": granted["stranger"].id}
    assert routes.change_owner(_Req(conn, granted["owner"], params={"id": str(sid)}, json=body)).status == 403
    assert routes.change_owner(_Req(conn, granted["admin"], params={"id": str(sid)}, json=body)).status == 204
    assert _one(conn, "SELECT owner_user_id FROM schedule WHERE id = %s", (sid,))["owner_user_id"] == body["user_id"]
    conv = _one(conn, "SELECT user_id FROM conversation WHERE id = %s", (ran.body["conversation_id"],))
    assert conv["user_id"] == body["user_id"]
    assert type(_Recorder.seen[-1]).__name__ == "ScheduleOwnerChangedEvent"


# --- Run now, delete ----------------------------------------------------------------------

def test_run_now_fires_through_tick_and_a_second_run_overlaps(conn, granted):
    sid = _create(conn, granted["owner"], view=granted["view"]).body["id"]
    req = _Req(conn, granted["owner"], params={"id": str(sid)})
    ran = routes.run_now(req)
    assert ran.status == 201, ran.body
    job = _one(conn, "SELECT * FROM job WHERE id = %s", (ran.body["job_id"],))
    assert (job["source"], job["type"], job["reference_id"], job["schedule_id"]) == (
        "scheduler", "scheduler", str(sid), sid)
    assert (job["requester_key"], job["requester_trust"]) == (f"user:{granted['owner'].id}", "account")
    assert job["context"].startswith(f'Scheduled task "Daily report" (schedule #{sid}).')
    conv = _one(conn, "SELECT * FROM conversation WHERE id = %s", (ran.body["conversation_id"],))
    assert (conv["channel"], conv["external_ref"], conv["user_id"], conv["thread_id"]) == (
        "scheduler", str(sid), granted["owner"].id, job["thread_id"])
    again = routes.run_now(req)
    assert (again.status, again.body["code"]) == (409, "overlap")
    assert "ScheduleRunEvent" in [type(e).__name__ for e in _Recorder.seen]


def test_delete_retitles_the_conversation_and_removes_the_row(conn, granted):
    sid = _create(conn, granted["owner"], view=granted["view"]).body["id"]
    conv_id = routes.run_now(_Req(conn, granted["owner"], params={"id": str(sid)})).body["conversation_id"]
    assert routes.delete(_Req(conn, granted["owner"], params={"id": str(sid)})).status == 204
    assert _one(conn, "SELECT 1 FROM schedule WHERE id = %s", (sid,)) is None
    assert _one(conn, "SELECT title FROM conversation WHERE id = %s", (conv_id,))["title"] == (
        "Daily report" + DELETED_SUFFIX)
    assert _one(conn, "SELECT action FROM schedule_audit WHERE schedule_id = %s ORDER BY id DESC LIMIT 1",
                (sid,))["action"] == "delete"
    assert type(_Recorder.seen[-1]).__name__ == "ScheduleDeletedEvent"


# --- channel seam ---------------------------------------------------------------------

def test_the_channel_gates_the_schedule_conversation(conn, granted):
    sid = _create(conn, granted["owner"], view=granted["view"]).body["id"]
    conv_id = routes.run_now(_Req(conn, granted["owner"], params={"id": str(sid)})).body["conversation_id"]
    channel = SchedulerChannel()
    conv = {"external_ref": str(sid)}
    assert channel.conversation_access(conn, granted["owner"], conv, "write") is True
    assert channel.conversation_access(conn, granted["stranger"], conv, "read") is True
    assert channel.conversation_access(conn, granted["stranger"], conv, "write") is False
    assert channel.conversation_access(conn, granted["owner"], conv, "delete") is False
    assert channel.conversation_access(conn, granted["owner"], {"external_ref": "999999"}, "read") is None
    with conn.cursor() as cur:
        assert channel.conversation_owner(cur, str(sid)) == (granted["owner"].id, "Daily report")
    conn.rollback()
    routes.put_member(_Req(conn, granted["owner"], params={"id": str(sid)}, json={"username": "c-stranger"}))
    thread_id = _one(conn, "SELECT thread_id FROM conversation WHERE id = %s", (conv_id,))["thread_id"]
    assert channel.shared_thread_ids(conn, granted["stranger"], before=None, limit=10) == [thread_id]
    assert channel.shared_thread_ids(conn, granted["stranger"], before=thread_id, limit=10) == []


# --- anchor reset (plan §5.1), panel writer; the tool writer is in test_scheduler_tools.py ---

def test_a_panel_start_change_resets_the_anchor(conn, granted, monkeypatch):
    clock = {"now": datetime(2026, 12, 31, 12, 0)}
    monkeypatch.setattr(store, "now_utc", lambda: clock["now"])
    sid = _create(conn, granted["owner"], view=granted["view"], rrule="FREQ=DAILY",
                  starts_at="2027-01-01T09:00", timezone="UTC").body["id"]
    for day in (1, 2):
        result = tick.tick(conn, now=datetime(2027, 1, day, 9, 0), batch=10)
        assert result.published == 1, result
        with conn.cursor() as cur:
            cur.execute("UPDATE job SET status = 'SUCCESS' WHERE schedule_id = %s", (sid,))
        conn.commit()
    clock["now"] = datetime(2027, 1, 2, 12, 0)
    req = _Req(conn, granted["owner"], params={"id": str(sid)}, json={"starts_at": "2027-01-01T10:00"})
    assert routes.update(req).status == 204
    assert tick.tick(conn, now=datetime(2027, 1, 3, 10, 0), batch=10).published == 1
    row = _one(conn, "SELECT last_fire_at, next_fire_at FROM schedule WHERE id = %s", (sid,))
    assert row == {"last_fire_at": datetime(2027, 1, 3, 10, 0), "next_fire_at": datetime(2027, 1, 4, 10, 0)}


# --- rights under the row lock (SEC-7): a change between the route's check and the write ------

def _meanwhile(monkeypatch, sql, args):
    """Commit ``sql`` on a second connection after the route's check, just before the store
    locks the row: what a concurrent request does."""
    real, done = tick.lock_row, []

    def hook(cur, schedule_id):
        if not done:
            done.append(True)
            other = _test_connection(autocommit=True)
            try:
                with other.cursor() as c:
                    c.execute(sql, args)
            finally:
                other.close()
        return real(cur, schedule_id)

    monkeypatch.setattr(tick, "lock_row", hook)


@pytest.mark.parametrize("op", ["update", "run_now"])
def test_a_share_revoked_before_the_lock_refuses_the_write(conn, granted, monkeypatch, op):
    sid = _create(conn, granted["owner"], view=granted["view"]).body["id"]
    routes.put_member(_Req(conn, granted["owner"], params={"id": str(sid)}, json={"username": "c-stranger"}))
    _meanwhile(monkeypatch, "DELETE FROM schedule_member WHERE schedule_id = %s", (sid,))
    req = _Req(conn, granted["stranger"], params={"id": str(sid)}, json={"title": "Edited", "rrule": "FREQ=DAILY"})
    response = getattr(routes, op)(req)
    assert response.status == 403, response.body  # still readable (scheduler.user), no longer writable
    assert _one(conn, "SELECT title FROM schedule WHERE id = %s", (sid,))["title"] == "Daily report"
    assert _one(conn, "SELECT COUNT(*) AS n FROM job WHERE schedule_id = %s", (sid,))["n"] == 0


@pytest.mark.parametrize("op,body", [
    ("delete", None), ("put_member", {"username": "c-admin"}), ("delete_member", None)])
def test_an_owner_change_before_the_lock_refuses_the_old_owner(conn, granted, monkeypatch, op, body):
    sid = _create(conn, granted["owner"], view=granted["view"]).body["id"]
    params = {"id": str(sid), "user_id": str(granted["admin"].id)}
    if op == "delete_member":
        routes.put_member(_Req(conn, granted["owner"], params=params, json={"username": "c-admin"}))
    _meanwhile(monkeypatch, "UPDATE schedule SET owner_user_id = %s WHERE id = %s", (granted["stranger"].id, sid))
    response = getattr(routes, op)(_Req(conn, granted["owner"], params=params, json=body))
    assert response.status == 403, response.body
    assert _one(conn, "SELECT deleted_at FROM schedule WHERE id = %s", (sid,))["deleted_at"] is None
    members = _one(conn, "SELECT COUNT(*) AS n FROM schedule_member WHERE schedule_id = %s", (sid,))["n"]
    assert members == (1 if op == "delete_member" else 0)


def test_an_admin_demoted_before_the_lock_cannot_change_the_owner(conn, granted, monkeypatch):
    sid = _create(conn, granted["owner"], view=granted["view"]).body["id"]
    _meanwhile(monkeypatch, "UPDATE `user` SET role = 'user' WHERE id = %s", (granted["admin"].id,))
    req = _Req(conn, granted["admin"], params={"id": str(sid)}, json={"user_id": granted["stranger"].id})
    assert routes.change_owner(req).status == 403
    assert _one(conn, "SELECT owner_user_id FROM schedule WHERE id = %s", (sid,))["owner_user_id"] == granted["owner"].id


# --- the target view is fixed after create (PLN-3: one schedule, one conversation) -----------

def test_the_target_view_cannot_change(conn, granted):
    sid = _create(conn, granted["owner"], view=granted["view"]).body["id"]
    moved = routes.update(_Req(conn, granted["owner"], params={"id": str(sid)},
                               json={"agent_view_id": granted["other_view"]}))
    assert (moved.status, moved.body["code"]) == (400, "view_fixed")
    same = _Req(conn, granted["owner"], params={"id": str(sid)}, json={"agent_view_id": granted["view"]})
    assert routes.update(same).status == 204
    assert _one(conn, "SELECT agent_view_id FROM schedule WHERE id = %s", (sid,))["agent_view_id"] == granted["view"]


# --- a post into the schedule's conversation re-checks the write at submission (SEC-7) -------

def _commit_aside(sql, args):
    other = _test_connection(autocommit=True)
    try:
        with other.cursor() as c:
            c.execute(sql, args)
    finally:
        other.close()


@pytest.mark.parametrize("hook", ["submit", "lock"])
@pytest.mark.parametrize("op", ["send", "regenerate"])
@pytest.mark.parametrize("change", ["unshare", "owner"])
def test_a_write_right_lost_after_the_route_check_refuses_the_post(conn, granted, monkeypatch, hook, op, change):
    """The route checks the write before the submission locks anything: a share removed or an
    owner changed in between refuses the post, with no message and no job. ``lock`` commits the
    change just before the schedule row lock: the re-check must read after that lock."""
    from agento.modules.conversation.src import routes as conv_routes
    from agento.modules.conversation.src import service as conv_service

    sid = _create(conn, granted["owner"], view=granted["view"]).body["id"]
    cid = routes.run_now(_Req(conn, granted["owner"], params={"id": str(sid)})).body["conversation_id"]
    routes.put_member(_Req(conn, granted["owner"], params={"id": str(sid)}, json={"username": "c-stranger"}))
    poster = granted["stranger"] if change == "unshare" else granted["owner"]
    first = conv_routes.send(_Req(conn, poster, params={"id": str(cid)},
                                  json={"client_message_id": "m1", "content": "first"}))
    assert first.status == 201, first.body
    sql, args = (("DELETE FROM schedule_member WHERE schedule_id = %s", (sid,)) if change == "unshare" else
                 ("UPDATE schedule SET owner_user_id = %s WHERE id = %s", (granted["admin"].id, sid)))
    if hook == "lock":
        _meanwhile(monkeypatch, sql, args)
    else:
        real = conv_service.submit_message

        def submit(*a, **k):
            _commit_aside(sql, args)
            return real(*a, **k)

        monkeypatch.setattr(conv_service, "submit_message", submit)
    messages = _one(conn, "SELECT COUNT(*) AS n FROM message WHERE conversation_id = %s", (cid,))["n"]
    jobs = _one(conn, "SELECT COUNT(*) AS n FROM job")["n"]
    body = ({"client_message_id": "m2", "content": "again"} if op == "send" else
            {"client_message_id": "m2", "message_id": first.body["message_id"]})
    response = getattr(conv_routes, op)(_Req(conn, poster, params={"id": str(cid)}, json=body))
    assert response.status == 403, response.body
    assert _one(conn, "SELECT COUNT(*) AS n FROM message WHERE conversation_id = %s", (cid,))["n"] == messages
    assert _one(conn, "SELECT COUNT(*) AS n FROM job")["n"] == jobs


# --- the per-user limit counts after the named lock, not in an older snapshot (F2) ------------

def test_two_creates_that_read_first_cannot_both_pass_the_limit(conn, granted):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO core_config_data (path, value) VALUES ('scheduler/limits/max_per_user', '1')")
    conn.commit()
    owner = granted["owner"]
    values = {"title": "t", "prompt": "p", "agent_view_id": granted["view"], "rrule": HOURLY,
              "starts_at": datetime(2026, 1, 1), "timezone": "UTC"}
    fires = store.validate(conn, HOURLY, values["starts_at"], "UTC")
    other = _test_connection()
    try:
        for c in (conn, other):  # what each route does before the save: a read opens its snapshot
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM schedule")
        store.save(conn, values, user=owner, actor="t", owner_user_id=owner.id, fires=fires, limit=1)
        with pytest.raises(store.StoreError) as refused:
            store.save(other, values, user=owner, actor="t", owner_user_id=owner.id, fires=fires, limit=1)
        assert refused.value.code == "limit"
    finally:
        other.close()
    assert _one(conn, "SELECT COUNT(*) AS n FROM schedule WHERE owner_user_id = %s", (owner.id,))["n"] == 1
