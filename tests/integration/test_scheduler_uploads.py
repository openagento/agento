"""The files of a schedule, through the routes and a real database (AG-87).

A file is staged (to the schedule, or to the draft bucket of a form not saved yet), a save
claims it into `schedule.attachments`, every fire publishes that descriptor, and the hourly
sweep is the one remover of a staged schedule file.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from agento.framework import artifacts_dir, uploads
from agento.framework.access import accounts
from agento.framework.artifacts_dir import attachment_block, deliver_attachments
from agento.framework.schedule import sources
from agento.modules.conversation.src import service as conversation
from agento.modules.scheduler.src import routes, store
from agento.modules.scheduler.src.source import SchedulerSource

from .conftest import _test_connection
from .test_conversation_submission import _Req

HOURLY = "FREQ=HOURLY;BYMINUTE=0"
OLD = time.time() - 48 * 3600


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(artifacts_dir, "BASE_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(sources, "load_sources", lambda: {"scheduler": SchedulerSource()})
    return tmp_path


@pytest.fixture
def grants(conn):
    for leaf in ("scheduler.own", "scheduler.own.write", "scheduler.own.share"):
        accounts.add_grant(conn, "user", "operation", leaf)


class _Upload(_Req):
    """A multipart POST: `web` hands the handler the raw body and the headers."""

    def __init__(self, conn, user, schedule_id=None, *, filename="report.pdf", payload=b"bytes"):
        super().__init__(conn, user, params={} if schedule_id is None else {"id": str(schedule_id)})
        self.body = (f'--B\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n\r\n'.encode()
                     + payload + b"\r\n--B--\r\n")
        self.headers = {"Content-Type": "multipart/form-data; boundary=B"}


def _create(conn, user, view, **extra):
    return routes.create(_Req(conn, user, json={"title": "t", "prompt": "p", "agent_view_id": view,
                                                "rrule": HOURLY, **extra}))


def _stage(conn, user, schedule_id=None, **kw) -> str:
    route = routes.upload_draft if schedule_id is None else routes.upload
    staged = route(_Upload(conn, user, schedule_id, **kw))
    assert staged.status == 201, staged.body
    return staged.body["upload_id"]


def _patch(conn, user, sid, ids):
    return routes.update(_Req(conn, user, params={"id": str(sid)}, json={"upload_ids": ids}))


def _descriptor(conn, sid) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT attachments FROM schedule WHERE id = %s", (sid,))
        raw = cur.fetchone()["attachments"]
    conn.rollback()
    return json.loads(raw) if raw else None


def _audits(conn, sid) -> list[tuple[str, dict | None]]:
    with conn.cursor() as cur:
        cur.execute("SELECT action, detail FROM schedule_audit WHERE schedule_id = %s ORDER BY id", (sid,))
        rows = [(r["action"], json.loads(r["detail"]) if r["detail"] else None) for r in cur.fetchall()]
    conn.rollback()
    return rows


def _age(workspace, owner: str, user_id: int, upload_id: str) -> None:
    os.utime(workspace / "uploads" / owner / f"u{user_id}" / upload_id, (OLD, OLD))


def _there(workspace, owner: str, user_id: int, upload_id: str) -> bool:
    return (workspace / "uploads" / owner / f"u{user_id}" / upload_id).is_dir()


# --- staging routes ----------------------------------------------------------------------

def test_the_upload_route_asks_for_the_schedules_write_right(conn, world, workspace):
    accounts.add_grant(conn, "user", "operation", "scheduler.own")
    sid = _create(conn, world["owner"], world["view"]).body["id"]
    assert routes.upload(_Upload(conn, world["stranger"], sid)).status == 404      # no read
    assert routes.upload(_Upload(conn, world["owner"], sid)).status == 403         # read, no write
    assert uploads.owners("s") == []
    accounts.add_grant(conn, "user", "operation", "scheduler.own.write")
    staged = routes.upload(_Upload(conn, world["owner"], sid))
    assert (staged.status, staged.body["name"], staged.body["bytes"]) == (201, "report.pdf", 5)
    assert _there(workspace, f"s{sid}", world["owner"].id, staged.body["upload_id"])


def test_a_file_over_the_cap_is_refused_with_the_upload_code(conn, world, workspace, grants):
    sid = _create(conn, world["owner"], world["view"]).body["id"]
    with conn.cursor() as cur:
        cur.execute("INSERT INTO core_config_data (scope, scope_id, path, value) "
                    "VALUES ('default', 0, 'core/uploads/max_bytes', '1024')")
    conn.commit()
    refused = routes.upload(_Upload(conn, world["owner"], sid, payload=b"x" * 2048))
    assert (refused.status, refused.body) == (413, {
        "error": "file too large (max 1 KB)", "code": "upload",
        "reason": "UPLOAD_TOO_LARGE", "params": {"max_bytes": 1024}})


def test_an_upload_refusal_with_no_params_names_its_reason_only(conn, world, workspace, grants):
    sid = _create(conn, world["owner"], world["view"]).body["id"]

    refused = routes.upload(_Upload(conn, world["owner"], sid, payload=b""))

    assert (refused.status, refused.body) == (400, {
        "error": "the file is empty", "code": "upload", "reason": "UPLOAD_EMPTY"})


def test_the_draft_route_asks_for_the_create_right(conn, world, workspace):
    assert routes.upload_draft(_Upload(conn, world["owner"])).status == 403
    assert uploads.owners("s") == []
    accounts.add_grant(conn, "user", "operation", "scheduler.own")
    staged = routes.upload_draft(_Upload(conn, world["owner"]))
    assert staged.status == 201
    assert _there(workspace, "s0", world["owner"].id, staged.body["upload_id"])


# --- create ------------------------------------------------------------------------------

def test_a_create_copies_the_draft_files_to_the_schedule(conn, world, workspace, grants):
    draft = _stage(conn, world["owner"], payload=b"the bytes")
    sid = _create(conn, world["owner"], world["view"], upload_ids=[draft]).body["id"]
    found = _descriptor(conn, sid)
    [entry] = found["files"]
    assert entry["as"] == "report.pdf"
    assert entry["from"].startswith(f"uploads/s{sid}/u{world['owner'].id}/")
    assert (workspace / entry["from"]).read_bytes() == b"the bytes"
    assert found["key"].startswith(f"s{sid}-") and len(found["key"]) == len(f"s{sid}-") + 12
    assert _there(workspace, "s0", world["owner"].id, draft)        # the sweep takes the draft, by age


def test_a_create_with_an_id_that_is_not_the_callers_draft_makes_no_schedule(conn, world, workspace, grants):
    theirs = _stage(conn, world["admin"])
    for bad in (theirs, "0" * 32):
        refused = _create(conn, world["owner"], world["view"], upload_ids=[bad])
        assert (refused.status, refused.body["code"]) == (400, "upload_gone")
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM schedule WHERE owner_user_id = %s", (world["owner"].id,))
        assert cur.fetchone()["n"] == 0
    conn.rollback()


@pytest.mark.parametrize("ids", ["x", ["not-an-id"], [7], ["a" * 32, "a" * 32], [f"{n:032x}" for n in range(11)]])
def test_upload_ids_that_are_not_a_short_list_of_distinct_ids_are_refused(conn, world, workspace, grants, ids):
    refused = _create(conn, world["owner"], world["view"], upload_ids=ids)
    assert (refused.status, refused.body["code"]) == (400, "bad_uploads")


# --- update ------------------------------------------------------------------------------

def test_an_update_adds_keeps_and_clears(conn, world, workspace, grants):
    sid = _create(conn, world["owner"], world["view"]).body["id"]
    first = _stage(conn, world["owner"], sid, filename="a.txt")
    assert _patch(conn, world["owner"], sid, [first]).status == 204
    one = _descriptor(conn, sid)

    assert _patch(conn, world["owner"], sid, [first]).status == 204           # the same set
    assert _descriptor(conn, sid) == one
    assert [a for a, _ in _audits(conn, sid)] == ["create", "update"]         # ...writes nothing
    assert _audits(conn, sid)[1][1] == {"fields": ["attachments"]}

    second = _stage(conn, world["owner"], sid, filename="b.txt")
    assert _patch(conn, world["owner"], sid, [first, second]).status == 204
    two = _descriptor(conn, sid)
    assert two["files"][0] == one["files"][0] and two["files"][1]["as"] == "b.txt"
    assert two["key"] != one["key"]

    assert routes.update(_Req(conn, world["owner"], params={"id": str(sid)}, json={"title": "new"})).status == 204
    assert _descriptor(conn, sid) == two                                      # absent: not changed

    assert _patch(conn, world["owner"], sid, []).status == 204
    assert _descriptor(conn, sid) is None


def test_a_member_keeps_the_owners_file_and_cannot_take_an_id_that_is_not_theirs(conn, world, workspace, grants):
    sid = _create(conn, world["owner"], world["view"]).body["id"]
    owners_file = _stage(conn, world["owner"], sid)
    _patch(conn, world["owner"], sid, [owners_file])
    unclaimed = _stage(conn, world["owner"], sid, filename="private.txt")
    store.share(conn, sid, world["stranger"].id, user=world["admin"], added=True, actor="test")

    mine = _stage(conn, world["stranger"], sid, filename="mine.txt")
    assert _patch(conn, world["stranger"], sid, [owners_file, mine]).status == 204
    assert [f["as"] for f in _descriptor(conn, sid)["files"]] == ["report.pdf", "mine.txt"]

    refused = _patch(conn, world["stranger"], sid, [owners_file, unclaimed])
    assert (refused.status, refused.body["code"]) == (400, "upload_gone")


def test_a_key_names_one_mapping_of_files_to_names_for_ever(conn, world, workspace, grants):
    """Delivery is done once per key, by NAME. Two files both called report.pdf: when the set
    is cleared and attached again in the other order, the names swap - so the key must change,
    or the next run would read the old bytes under the new names."""
    sid = _create(conn, world["owner"], world["view"]).body["id"]
    a = _stage(conn, world["owner"], sid, payload=b"AAAA")
    b = _stage(conn, world["owner"], sid, payload=b"BBBB")
    _patch(conn, world["owner"], sid, [a, b])
    ab = _descriptor(conn, sid)
    _patch(conn, world["owner"], sid, [b, a])
    reordered = _descriptor(conn, sid)                       # names kept: A=report, B=report-2
    _patch(conn, world["owner"], sid, [])
    _patch(conn, world["owner"], sid, [b, a])
    again = _descriptor(conn, sid)                           # names given anew: B=report, A=report-2

    assert [f["as"] for f in reordered["files"]] == ["report-2.pdf", "report.pdf"]
    assert [f["as"] for f in again["files"]] == ["report.pdf", "report-2.pdf"]
    assert len({ab["key"], reordered["key"], again["key"]}) == 3

    shared = workspace / "thread" / "shared"
    for found in (reordered, again):
        deliver_attachments(json.dumps(found), shared, gid=os.getgid())
        got = {name: (shared / "attachments" / found["key"] / name).read_bytes()
               for name in ("report.pdf", "report-2.pdf")}
        assert got == {f["as"]: (workspace / f["from"]).read_bytes() for f in found["files"]}


def test_the_row_shows_names_and_sizes_and_never_a_path(conn, world, workspace, grants):
    draft = _stage(conn, world["owner"], payload=b"12345678")
    sid = _create(conn, world["owner"], world["view"], upload_ids=[draft]).body["id"]
    shown = routes.get_schedule(_Req(conn, world["owner"], params={"id": str(sid)})).body["attachments"]
    assert [(f["name"], f["bytes"]) for f in shown] == [("report.pdf", 8)]
    assert set(shown[0]) == {"upload_id", "name", "bytes"}
    assert _patch(conn, world["owner"], sid, [shown[0]["upload_id"]]).status == 204   # the id round-trips

    uploads.remove(f"s{sid}", world["owner"].id, shown[0]["upload_id"])
    gone = routes.get_schedule(_Req(conn, world["owner"], params={"id": str(sid)})).body["attachments"]
    assert gone[0]["bytes"] is None
    listed = routes.list_schedules(_Req(conn, world["owner"])).body["groups"]
    assert all("attachments" not in row for rows in listed.values() for row in rows)


# --- the fire ----------------------------------------------------------------------------

def test_a_fire_delivers_the_files_and_the_prompt_names_them(conn, world, workspace, grants):
    draft = _stage(conn, world["owner"], payload=b"quarterly numbers")
    sid = _create(conn, world["owner"], world["view"], upload_ids=[draft]).body["id"]
    ran = routes.run_now(_Req(conn, world["owner"], params={"id": str(sid)}))
    assert ran.status == 201, ran.body
    with conn.cursor() as cur:
        cur.execute("SELECT attachments FROM job WHERE id = %s", (ran.body["job_id"],))
        raw = cur.fetchone()["attachments"]
    conn.rollback()
    found = _descriptor(conn, sid)
    assert json.loads(raw) == found

    shared = workspace / "thread" / "shared"
    deliver_attachments(raw, shared, gid=os.getgid())                 # what run preparation does
    assert (shared / "attachments" / found["key"] / "report.pdf").read_bytes() == b"quarterly numbers"
    assert f"- ./shared/attachments/{found['key']}/report.pdf" in attachment_block(raw)


# --- the sweep ---------------------------------------------------------------------------

def test_the_sweep_takes_only_old_dirs_that_nothing_names(conn, world, workspace, grants):
    owner_id = world["owner"].id
    sid = _create(conn, world["owner"], world["view"]).body["id"]
    named = _stage(conn, world["owner"], sid, filename="named.txt")
    replaced = _stage(conn, world["owner"], sid, filename="replaced.txt")
    young = _stage(conn, world["owner"], sid, filename="young.txt")
    abandoned = _stage(conn, world["owner"], sid, filename="abandoned.txt")
    old_draft, young_draft = _stage(conn, world["owner"]), _stage(conn, world["owner"])
    _patch(conn, world["owner"], sid, [replaced])
    assert routes.run_now(_Req(conn, world["owner"], params={"id": str(sid)})).status == 201  # an open job
    _patch(conn, world["owner"], sid, [named])
    for upload_id in (named, replaced, abandoned):
        _age(workspace, f"s{sid}", owner_id, upload_id)
    _age(workspace, "s0", owner_id, old_draft)

    assert store.sweep_uploads(conn, retention_hours=24) == 2
    left = {u: _there(workspace, f"s{sid}", owner_id, u) for u in (named, replaced, young, abandoned)}
    assert left == {named: True, replaced: True, young: True, abandoned: False}   # the open job keeps `replaced`
    assert not _there(workspace, "s0", owner_id, old_draft) and _there(workspace, "s0", owner_id, young_draft)

    with conn.cursor() as cur:
        cur.execute("UPDATE job SET status = 'SUCCESS' WHERE schedule_id = %s", (sid,))
    conn.commit()
    assert store.sweep_uploads(conn, retention_hours=24) == 1
    assert not _there(workspace, f"s{sid}", owner_id, replaced)


def test_the_sweep_sees_a_run_fired_after_its_caller_last_read(conn, world, workspace, grants):
    """The command reads the retention first, which opens a read snapshot. A fire and a replace
    that commit after it must still count: the open run names the replaced files."""
    sid = _create(conn, world["owner"], world["view"]).body["id"]
    fired, later = _stage(conn, world["owner"], sid), _stage(conn, world["owner"], sid, filename="later.txt")
    _patch(conn, world["owner"], sid, [fired])
    _age(workspace, f"s{sid}", world["owner"].id, fired)
    retention = int(store.setting(conn, "limits/upload_retention_hours"))     # the snapshot opens here

    other = _test_connection(autocommit=False)
    try:
        assert routes.run_now(_Req(other, world["owner"], params={"id": str(sid)})).status == 201
        assert _patch(other, world["owner"], sid, [later]).status == 204
    finally:
        other.close()

    assert store.sweep_uploads(conn, retention_hours=retention) == 0
    assert _there(workspace, f"s{sid}", world["owner"].id, fired)


def test_the_sweep_keeps_a_tree_whose_descriptor_does_not_parse(conn, world, workspace, grants):
    """One bad row must not stop the sweep, and its files stay: nothing proves them unnamed."""
    bad = _create(conn, world["owner"], world["view"]).body["id"]
    good = _create(conn, world["owner"], world["view"]).body["id"]
    kept, gone = _stage(conn, world["owner"], bad), _stage(conn, world["owner"], good)
    with conn.cursor() as cur:
        cur.execute("UPDATE schedule SET attachments = '{not json' WHERE id = %s", (bad,))
    conn.commit()
    _age(workspace, f"s{bad}", world["owner"].id, kept)
    _age(workspace, f"s{good}", world["owner"].id, gone)

    assert store.sweep_uploads(conn, retention_hours=24) == 1
    assert _there(workspace, f"s{bad}", world["owner"].id, kept)
    assert not _there(workspace, f"s{good}", world["owner"].id, gone)


def test_a_delete_leaves_the_files_to_the_sweep(conn, world, workspace, grants):
    """The delete transaction can still roll back, so it removes no file. The sweep does, when
    no row names the tree any more."""
    sid = _create(conn, world["owner"], world["view"]).body["id"]
    upload_id = _stage(conn, world["owner"], sid)
    _patch(conn, world["owner"], sid, [upload_id])
    assert routes.delete(_Req(conn, world["owner"], params={"id": str(sid)})).status == 204
    assert _there(workspace, f"s{sid}", world["owner"].id, upload_id)

    assert store.sweep_uploads(conn, retention_hours=24) == 0            # still young
    _age(workspace, f"s{sid}", world["owner"].id, upload_id)
    assert store.sweep_uploads(conn, retention_hours=24) == 1
    assert not (workspace / "uploads" / f"s{sid}").exists()              # the empty tree goes too


def test_the_conversation_sweep_leaves_a_schedules_files_alone(conn, world, workspace, grants):
    """Both sweeps walk one `uploads/` root. A schedule's files have no message that names
    them, so the conversation sweep must not see them at all."""
    sid = _create(conn, world["owner"], world["view"]).body["id"]
    upload_id = _stage(conn, world["owner"], sid)
    _age(workspace, f"s{sid}", world["owner"].id, upload_id)
    assert conversation.sweep_uploads(conn, retention_hours=1) == 0
    assert _there(workspace, f"s{sid}", world["owner"].id, upload_id)
