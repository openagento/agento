"""The conversation access seam (AG-67 plan §8): a channel may own, share and guard its
conversations through optional members (`framework/channels/base.py`), and a message
carries its poster, who is the only requester a reply is published as (F19).
"""
from __future__ import annotations

import uuid

import pytest

from agento.framework.access import accounts
from agento.framework.channels import registry
from agento.framework.channels.base import PromptFragments
from agento.framework.job_models import JobRequester, RequesterTrust
from agento.framework.job_types import resolve_job_type
from agento.framework.publish_service import publish_job
from agento.framework.threads import ensure_thread
from agento.modules.conversation.src import retention, routes, service

from .conftest import _test_connection
from .test_conversation_submission import _Req
from .test_conversation_threads import _run

NAME = "fakech"


class _Fake:
    """A channel that answers the seam from its attributes."""

    def __init__(self) -> None:
        self.answers: dict[str, bool | None] = {}
        self.owner: tuple[int | None, str] | None = None
        self.shared: list[int] = []
        self.calls: list[dict] = []
        self.boom = False

    @property
    def name(self) -> str:
        return NAME

    def get_prompt_fragments(self, reference_id, config=None):
        return PromptFragments(read_context="r", respond="r")

    def get_followup_fragments(self, reference_id, instructions, config=None):
        return PromptFragments(read_context="r", respond="r")

    def conversation_owner(self, cur, reference_id):
        if self.boom:
            raise RuntimeError("down")
        return self.owner

    def conversation_access(self, conn, user, conversation, mode):
        if self.boom:
            raise RuntimeError("down")
        return self.answers.get(mode)

    def shared_thread_ids(self, conn, user, *, before, limit):
        self.calls.append({"before": before, "limit": limit})
        ids = sorted((i for i in self.shared if before is None or i < before), reverse=True)
        return ids[:limit]


@pytest.fixture
def fake(monkeypatch) -> _Fake:
    channel = _Fake()
    monkeypatch.setitem(registry._CHANNELS, NAME, channel)
    return channel


def _rows(conn, sql, args=()) -> list[dict]:
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = list(cur.fetchall())
    conn.commit()
    return rows


def _channel_conversation(conn, world, *, owner=None, channel=NAME, view="view",
                          ref=None) -> int:
    ref = ref or str(uuid.uuid4())
    with conn.cursor() as cur:
        thread_id = ensure_thread(cur, agent_view_id=world[view], ref=f"{channel}:{ref}")
        cid = service.ensure_channel_conversation(
            cur, thread_id=thread_id, agent_view_id=world[view], channel=channel,
            external_ref=ref, user_id=owner.id if owner else None, title="t")
    conn.commit()
    return cid


def _post(conn, user, cid, cmid="m1"):
    return routes.send(_Req(conn, user, params={"id": str(cid)},
                            json={"client_message_id": cmid, "content": "hej"}))


# --- read ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer, readable", [(True, True), (False, False), (None, False)])
def test_the_channel_decides_whether_a_non_owner_reads(conn, world, fake, answer, readable):
    cid = _channel_conversation(conn, world, owner=world["owner"])
    fake.answers["read"] = answer

    seen = service.load_visible(conn, conversation_id=cid, user=world["stranger"])

    assert (seen is not None) is readable


def test_a_shared_read_still_needs_reach_over_the_view(conn, world, fake):
    cid = _channel_conversation(conn, world, owner=world["owner"], view="other_view")
    fake.answers["read"] = True

    assert service.load_visible(conn, conversation_id=cid, user=world["stranger"]) is None


@pytest.mark.parametrize("broken", ["unknown", "raises"])
def test_an_unknown_or_failing_channel_grants_nothing_and_keeps_the_existing_gate(
        conn, world, fake, broken):
    channel = "nochannel" if broken == "unknown" else NAME
    fake.boom = broken == "raises"
    fake.answers["read"] = True
    cid = _channel_conversation(conn, world, owner=world["owner"], channel=channel)

    assert service.load_visible(conn, conversation_id=cid, user=world["stranger"]) is None
    assert service.load_visible(conn, conversation_id=cid, user=world["owner"]) is not None
    assert service.load_visible(conn, conversation_id=cid, user=world["admin"]) is not None


def test_the_seam_is_never_asked_about_a_panel_conversation(conn, world, fake):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    fake.answers["read"] = True

    assert service.load_visible(conn, conversation_id=cid, user=world["stranger"]) is None


# --- write --------------------------------------------------------------------------

@pytest.mark.parametrize("answer, status", [(True, 201), (False, 403), (None, 403)])
def test_the_channel_may_grant_a_post(conn, world, fake, answer, status):
    cid = _channel_conversation(conn, world, owner=world["owner"])
    fake.answers.update(read=True, write=answer)

    assert _post(conn, world["stranger"], cid).status == status


@pytest.mark.parametrize("op", ["send", "regenerate"])
def test_a_write_lost_after_the_route_check_refuses_the_post(conn, world, fake, monkeypatch, op):
    """SEC-7, every channel: the submission asks the write gate again, after the route's check.
    A channel that stops granting the write in between refuses the post: no message, no job."""
    cid = _channel_conversation(conn, world, owner=world["owner"])
    fake.answers.update(read=True, write=True)
    first = _post(conn, world["stranger"], cid)
    assert first.status == 201, first.body
    real = service.submit_message

    def submit(*a, **k):
        fake.answers["write"] = False
        return real(*a, **k)

    monkeypatch.setattr(service, "submit_message", submit)
    messages = _rows(conn, "SELECT COUNT(*) AS n FROM message WHERE conversation_id = %s", (cid,))[0]["n"]
    jobs = _rows(conn, "SELECT COUNT(*) AS n FROM job")[0]["n"]
    body = ({"client_message_id": "m2", "content": "again"} if op == "send" else
            {"client_message_id": "m2", "message_id": first.body["message_id"]})
    assert getattr(routes, op)(_Req(conn, world["stranger"], params={"id": str(cid)}, json=body)).status == 403
    assert _rows(conn, "SELECT COUNT(*) AS n FROM message WHERE conversation_id = %s", (cid,))[0]["n"] == messages
    assert _rows(conn, "SELECT COUNT(*) AS n FROM job")[0]["n"] == jobs


def test_a_post_and_a_pin_of_one_owner_do_not_deadlock(conn, world, fake):
    """SCL-1: the message's author key takes a shared lock on the poster's user row, and a pin
    locks that user row before the conversation. A post that took the conversation first and
    the user row only at its INSERT would close a cycle with a pin. Here the pin's steps run on
    a second connection around a post that runs on its own thread."""
    import threading
    import time

    cid = _channel_conversation(conn, world, owner=world["owner"])
    fake.answers.update(read=True, write=True)
    pin = _test_connection()
    poster = _test_connection()
    result: dict = {}

    def post():
        try:
            result["status"] = _post(poster, world["owner"], cid).status
        except Exception as exc:  # a deadlock victim
            result["error"] = exc

    try:
        with pin.cursor() as cur:
            cur.execute("SELECT id FROM `user` WHERE id = %s FOR UPDATE", (world["owner"].id,))
            worker = threading.Thread(target=post)
            worker.start()
            time.sleep(1.0)  # the post reaches its lock wait
            cur.execute("SELECT id FROM conversation WHERE id = %s FOR UPDATE", (cid,))
        pin.commit()
        worker.join(30)
    finally:
        pin.close()
        poster.close()
    assert result == {"status": 201}


def test_a_failing_conversation_lock_refuses_the_post(conn, world, fake):
    """SEC-9: a channel lock member that raises grants nothing."""
    cid = _channel_conversation(conn, world, owner=world["owner"])
    fake.answers.update(read=True, write=True)

    def boom(cur, conversation):
        raise RuntimeError("down")

    fake.conversation_lock = boom
    assert _post(conn, world["stranger"], cid).status == 403
    assert _rows(conn, "SELECT COUNT(*) AS n FROM message WHERE conversation_id = %s", (cid,))[0]["n"] == 0


def test_show_tells_an_owned_channel_conversation_reader_whether_they_may_post(
        conn, world, fake):
    cid = _channel_conversation(conn, world, owner=world["owner"])
    fake.answers["write"] = True

    body = routes.show(_Req(conn, world["owner"], params={"id": str(cid)})).body

    assert body["channel_write"] is True


# --- rename, pin, archive (D-32) -------------------------------------------------------

def test_a_reader_by_share_may_not_rename_pin_or_archive(conn, world, fake):
    cid = _channel_conversation(conn, world, owner=world["owner"])
    fake.answers["read"] = True
    req = {"params": {"id": str(cid)}}

    assert routes.update(_Req(conn, world["stranger"], json={"title": "x"}, **req)).status == 403
    assert routes.destroy(_Req(conn, world["stranger"], **req)).status == 403
    assert _rows(conn, "SELECT title, status FROM conversation WHERE id = %s", (cid,))[0] \
        == {"title": "t", "status": "active"}


def test_the_owner_of_a_channel_conversation_may_rename_pin_and_archive(conn, world, fake):
    cid = _channel_conversation(conn, world, owner=world["owner"])
    req = {"params": {"id": str(cid)}}

    assert routes.update(_Req(conn, world["owner"], json={"title": "new", "pinned": True},
                              **req)).status == 200
    assert routes.destroy(_Req(conn, world["owner"], **req)).status == 200
    row = _rows(conn, "SELECT title, status, pinned_at FROM conversation WHERE id = %s", (cid,))[0]
    assert (row["title"], row["status"], row["pinned_at"] is not None) == ("new", "archived", True)


@pytest.mark.parametrize("op, body", [("update", {"title": "x"}), ("update", {"pinned": True}),
                                      ("destroy", None)])
def test_an_owner_change_after_the_check_refuses_the_old_owner(conn, world, fake, monkeypatch, op, body):
    """SEC-7: the owner decides under the row lock, not at the route's check."""
    cid = _channel_conversation(conn, world, owner=world["owner"])
    fake.answers["read"] = True
    real = routes._may_manage

    def meanwhile(req, row, *, admin):
        allowed = real(req, row, admin=admin)
        other = _test_connection(autocommit=True)
        try:
            with other.cursor() as cur:
                cur.execute("UPDATE conversation SET user_id = %s WHERE id = %s", (world["stranger"].id, cid))
        finally:
            other.close()
        return allowed

    monkeypatch.setattr(routes, "_may_manage", meanwhile)
    assert getattr(routes, op)(_Req(conn, world["owner"], params={"id": str(cid)}, json=body)).status == 403
    assert _rows(conn, "SELECT title, status, pinned_at FROM conversation WHERE id = %s", (cid,))[0] \
        == {"title": "t", "status": "active", "pinned_at": None}


def test_an_ownerless_channel_conversation_still_has_nothing_to_rename(conn, world, fake):
    cid = _channel_conversation(conn, world)

    assert routes.update(_Req(conn, world["admin"], params={"id": str(cid)},
                              json={"title": "x"})).status == 403


# --- delete ---------------------------------------------------------------------------

@pytest.mark.parametrize("channel, answer, deleted", [
    (NAME, False, False), (NAME, None, True), (NAME, True, True), ("nochannel", None, False)])
def test_the_channel_may_refuse_a_delete(conn, world, fake, channel, answer, deleted):
    cid = _channel_conversation(conn, world, owner=world["owner"], channel=channel)
    fake.answers["delete"] = answer

    assert service.delete_conversation(conn, cid) is deleted
    assert bool(_rows(conn, "SELECT id FROM conversation WHERE id = %s", (cid,))) is not deleted


def test_a_refused_delete_does_not_starve_the_retention_batch(conn, world, fake):
    """A refused row moves to the back of the queue, so the next pass reaches the others."""
    refused = [_channel_conversation(conn, world, owner=world["owner"]) for _ in range(2)]
    free = service.create_conversation(conn, user_id=world["owner"].id,
                                       agent_view_id=world["view"], title="t")
    fake.answers["delete"] = False
    with conn.cursor() as cur:
        cur.execute("UPDATE conversation SET status = 'archived', "
                    "updated_at = NOW() - INTERVAL 10 DAY")
        cur.execute("UPDATE conversation SET updated_at = NOW() - INTERVAL 20 DAY "
                    "WHERE id IN (%s, %s)", refused)
    conn.commit()

    first = retention.delete_archived(conn, archived_days=5, limit=2)
    second = retention.delete_archived(conn, archived_days=5, limit=2)

    assert (first, second) == (0, 1)
    assert [r["id"] for r in _rows(conn, "SELECT id FROM conversation ORDER BY id")] == refused
    assert free not in [r["id"] for r in _rows(conn, "SELECT id FROM conversation")]


def test_a_disabled_channels_conversations_take_no_slot_in_the_retention_batch(conn, world):
    kept = [_channel_conversation(conn, world, owner=world["owner"], channel="nochannel")
            for _ in range(3)]
    free = service.create_conversation(conn, user_id=world["owner"].id,
                                       agent_view_id=world["view"], title="t")
    with conn.cursor() as cur:
        cur.execute("UPDATE conversation SET status = 'archived', "
                    "updated_at = NOW() - INTERVAL 10 DAY")
        cur.execute("UPDATE conversation SET updated_at = NOW() - INTERVAL 20 DAY "
                    "WHERE id <> %s", (free,))
    conn.commit()

    assert retention.delete_archived(conn, archived_days=5, limit=1) == 1
    assert [r["id"] for r in _rows(conn, "SELECT id FROM conversation ORDER BY id")] == kept


# --- the owner of a new channel conversation -------------------------------------------

def _publish(world, *, reference="7", source=NAME) -> int:
    return publish_job(source=source, agent_type=resolve_job_type("followup"),
                       agent_view_id=world["view"], reference_id=reference,
                       idempotency_key=str(uuid.uuid4()),
                       requester=JobRequester(key="t", trust=RequesterTrust.CLAIMED),
                       priority=50)


def test_the_channel_names_the_owner_and_title_of_its_new_conversation(conn, world, fake):
    fake.owner = (world["owner"].id, "Daily report")
    cid = _run(conn, _publish(world))

    fake.owner = (world["stranger"].id, "Renamed")             # a duplicate changes nothing
    assert _run(conn, _publish(world)) == cid
    row = _rows(conn, "SELECT user_id, title, channel FROM conversation WHERE id = %s", (cid,))[0]
    assert row == {"user_id": world["owner"].id, "title": "Daily report", "channel": NAME}


@pytest.mark.parametrize("state", ["silent", "raises", "gone_user", "bad_shape"])
def test_a_channel_without_an_owner_answer_gives_an_ownerless_conversation(
        conn, world, fake, state):
    fake.boom = state == "raises"
    fake.owner = {"gone_user": (987654321, "t"), "bad_shape": (1,)}.get(state)  # type: ignore[assignment]
    cid = _run(conn, _publish(world))

    row = _rows(conn, "SELECT user_id, title FROM conversation WHERE id = %s", (cid,))[0]
    assert row == {"user_id": None, "title": f"{NAME} 7"}


def test_retitle_and_set_channel_owner_change_the_threads_conversation(conn, world, fake):
    cid = _channel_conversation(conn, world, owner=world["owner"])
    service.update_thread(conn, cid, user_id=world["owner"].id, pinned=True)
    thread_id = _rows(conn, "SELECT thread_id FROM conversation WHERE id = %s", (cid,))[0]["thread_id"]

    with conn.cursor() as cur:
        service.retitle(cur, thread_id, "x" * 300)
        service.set_channel_owner(cur, thread_id, world["stranger"].id)
    conn.commit()

    row = _rows(conn, "SELECT user_id, title, pinned_at FROM conversation WHERE id = %s", (cid,))[0]
    assert row == {"user_id": world["stranger"].id, "title": "x" * service.MAX_TITLE,
                   "pinned_at": None}


# --- the shared list (D-30) ------------------------------------------------------------

def test_the_shared_list_pages_250_shares_from_many_owners(conn, world, fake):
    owners = [accounts.create_user(conn, f"c-o{n}", "user", "pw-owner-123456") for n in range(5)]
    reachable, threads = [], []
    for n in range(250):
        view = "other_view" if n % 25 == 0 else "view"          # 10 lost to reach
        cid = _channel_conversation(conn, world, owner=owners[n % 5], view=view)
        thread_id = _rows(conn, "SELECT thread_id FROM conversation WHERE id = %s",
                          (cid,))[0]["thread_id"]
        threads.append(thread_id)
        if view == "view":
            reachable.append(cid)
    with conn.cursor() as cur:                    # 10 shares with no conversation yet
        for n in range(10):
            threads.append(ensure_thread(cur, agent_view_id=world["view"], ref=f"{NAME}:x{n}"))
    conn.commit()
    fake.shared = threads
    fake.answers["read"] = True

    seen, before, pages = [], None, 0
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "config", lambda conn, path: 40)
        while True:
            query = {"scope": "shared", **({"before": str(before)} if before else {})}
            body = routes.index(_Req(conn, world["stranger"], query=query)).body
            seen += [c["id"] for c in body["conversations"]]
            pages += 1
            if body["next"] is None:
                break
            before = body["next"]

    assert sorted(seen) == sorted(reachable) and len(seen) == len(set(seen))
    assert pages == 7                                  # 260 ids, 40 a page
    assert all(call["limit"] == 40 for call in fake.calls)


def test_the_shared_list_skips_a_failing_channel(conn, world, fake):
    fake.shared = [1]
    fake.calls.clear()

    def boom(*a, **k):
        raise RuntimeError("down")

    fake.shared_thread_ids = boom  # type: ignore[method-assign]
    _channel_conversation(conn, world, owner=world["owner"])

    body = routes.index(_Req(conn, world["stranger"], query={"scope": "shared"})).body

    assert body == {"conversations": [], "next": None}


# --- the requester is the poster (F19) ---------------------------------------------------

def test_a_reply_in_a_channel_thread_is_published_as_its_poster(conn, world, fake):
    cid = _channel_conversation(conn, world)

    sent = _post(conn, world["admin"], cid)

    job = _rows(conn, "SELECT requester_key, requester_trust FROM job WHERE id = %s",
                (sent.body["job_id"],))[0]
    assert job == {"requester_key": f"user:{world['admin'].id}", "requester_trust": "account"}
    assert _rows(conn, "SELECT author_user_id FROM message")[0]["author_user_id"] \
        == world["admin"].id


def test_an_author_deleted_before_publication_is_never_replaced_by_the_owner(
        conn, world, monkeypatch):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    poster = accounts.create_user(conn, "c-poster", "admin", "pw-poster-12345")

    def down(**kwargs):
        raise RuntimeError("the publisher died")

    publishing = service.publish_job
    monkeypatch.setattr(service, "publish_job", down)
    with pytest.raises(RuntimeError):
        service.submit_message(conn, conversation_id=cid, user_id=poster.id,
                               client_message_id="c1", content="hej")
    monkeypatch.setattr(service, "publish_job", publishing)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM `user` WHERE id = %s", (poster.id,))
    conn.commit()

    assert service.sweep_pending(conn, grace_seconds=0) == 1

    job = _rows(conn, "SELECT requester_key, requester_trust FROM job")[0]
    assert job == {"requester_key": None, "requester_trust": "claimed"}


# --- retention clock (CLS-1: the channel, not the owner, picks the branch) ----------------

def test_an_owned_channel_conversation_with_a_running_run_is_not_idle(conn, world, fake):
    cid = _channel_conversation(conn, world, owner=world["owner"])
    with conn.cursor() as cur:
        cur.execute("UPDATE conversation SET created_at = NOW() - INTERVAL 100 DAY, "
                    "last_activity_at = NOW() - INTERVAL 100 DAY WHERE id = %s", (cid,))
        cur.execute("INSERT INTO execution (execution_id, job_id, attempt, status, "
                    "conversation_id) VALUES ('e-run', 1, 1, 'running', %s)", (cid,))
    conn.commit()

    assert retention.auto_archive(conn, idle_days=90) == 0
