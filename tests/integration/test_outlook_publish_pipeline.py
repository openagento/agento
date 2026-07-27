"""Integration: the Outlook per-agent_view publisher loop against real MySQL + real config resolution.

Unit tests mock the framework helpers; this proves the loop fans each mailbox's messages to the
correct agent_view, honours per-view allowed_senders/DMARC, dedupes a shared mailbox, and persists
the per-mailbox delta cursor — against real agent_view + core_config_data + job + outlook_poll_cursor
rows (only the toolbox Graph fetch is stubbed via respx).
"""
from __future__ import annotations

import json
import logging

import pytest
import respx
from httpx import Response

from agento.framework import router_registry
from agento.framework.ingress_identity import (
    bind_identity,
    is_regex_identity_type,
    register_regex_identity_type,
)
from agento.framework.scoped_config import Scope, scoped_config_set
from agento.modules.core.src.routers.identity_router import IdentityRouter
from agento.modules.outlook.src.commands.publish import publish_all_views
from agento.modules.outlook.src.cursor import load_cursors

from .conftest import _test_connection, fetch_all_jobs


@pytest.fixture
def routing_registered():
    """Ensure the outlook_sender regex identity type + the IdentityRouter are registered so
    resolve_agent_view() runs in routed-mode tests (the session bootstrap already registers both,
    but this makes the test self-contained and order-independent). Restores prior state on teardown."""
    from agento.framework.ingress_identity import _REGEX_IDENTITY_TYPES

    saved_types = set(_REGEX_IDENTITY_TYPES)
    register_regex_identity_type("outlook_sender")
    if not any(getattr(r, "name", None) == "identity" for r in router_registry.get_routers()):
        router_registry.register_router(IdentityRouter(), order=100)
    yield
    _REGEX_IDENTITY_TYPES.clear()
    _REGEX_IDENTITY_TYPES.update(saved_types)

ALLOWED = "sklep@mycompanystudio.com, ops@mycompany.com"
TOOLBOX_URL = "http://toolbox:3001"
DELTA_URL = f"{TOOLBOX_URL}/api/outlook/delta"


@pytest.fixture(autouse=True)
def _clean_cursors():
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM outlook_poll_cursor")
        yield
        with conn.cursor() as cur:
            cur.execute("DELETE FROM outlook_poll_cursor")
    finally:
        conn.close()


@pytest.fixture
def two_views():
    """Create one workspace + two active agent_views, each with scoped outlook config (enabled +
    allowed_senders + a distinct mailbox UPN so each is a size-1 DIRECT-mode group). Returns
    (dev_id, ops_id). Cleans up workspace (cascades to agent_view + its ingress_identity rows) and
    the agent_view-scoped core_config_data rows it wrote — none are in the autouse truncation set."""
    conn = _test_connection(autocommit=True)
    av_ids: list[int] = []
    mailboxes = {"av-outlook-dev": "dev@example.com", "av-outlook-ops": "ops@example.com"}
    codes = {}
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO workspace (code, label) VALUES ('ws-outlook-pv', 'outlook pv')")
            ws_id = cur.lastrowid
            for code in ("av-outlook-dev", "av-outlook-ops"):
                cur.execute(
                    "INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, %s, %s)",
                    (ws_id, code, code),
                )
                av_ids.append(cur.lastrowid)
                codes[cur.lastrowid] = code
        for av_id in av_ids:
            scoped_config_set(conn, "outlook/enabled", "1", scope=Scope.AGENT_VIEW, scope_id=av_id)
            scoped_config_set(conn, "outlook/allowed_senders", ALLOWED, scope=Scope.AGENT_VIEW, scope_id=av_id)
            scoped_config_set(conn, "outlook/outlook_mailbox_user_id", mailboxes[codes[av_id]],
                              scope=Scope.AGENT_VIEW, scope_id=av_id)
        conn.commit()
        yield av_ids[0], av_ids[1]
    finally:
        with conn.cursor() as cur:
            for av_id in av_ids:
                cur.execute(
                    "DELETE FROM core_config_data WHERE scope = 'agent_view' AND scope_id = %s",
                    (av_id,),
                )
            cur.execute("DELETE FROM workspace WHERE code = 'ws-outlook-pv'")
        conn.close()


def _delta_stub(by_view, *, delta_link="L-NEXT"):
    """Return a respx side_effect that maps the posted agent_view_id -> {mailbox, messages, deltaLink}.

    Each message is enriched with the activation-relevant fields (to/cc/bodyPreview/agent_authored/
    receivedDateTime) the delta handler now emits. A message with no explicit ``to`` defaults to being
    addressed directly at the mailbox (the common case: sole recipient -> direct activation), so the
    existing gate/dedup/cursor assertions keep their meaning; new tests override to/cc/bodyPreview.
    """
    def _handler(request):
        payload = json.loads(request.content)
        src = by_view[payload["agent_view_id"]]
        mailbox = src.get("mailbox")
        messages = []
        for m in src.get("messages", []):
            msg = dict(m)
            msg.setdefault("to", [{"name": "", "address": mailbox}] if mailbox else [])
            msg.setdefault("cc", [])
            msg.setdefault("bodyPreview", "")
            msg.setdefault("agent_authored", False)
            msg.setdefault("receivedDateTime", "2026-01-01T00:00:00Z")
            messages.append(msg)
        out = dict(src)
        out["messages"] = messages
        out.setdefault("deltaLink", delta_link)
        out.setdefault("resynced", False)
        return Response(200, json=out)
    return _handler


@respx.mock
def test_multi_view_fans_each_mailbox_to_correct_view(int_db_config, two_views):
    dev_id, ops_id = two_views
    by_view = {
        dev_id: {"mailbox": "dev@example.com", "messages": [
            {"id": "m-dev", "from": {"address": "sklep@mycompanystudio.com"}, "dmarc": "pass"}]},
        ops_id: {"mailbox": "ops@example.com", "messages": [
            {"id": "m-ops", "from": {"address": "ops@mycompany.com"}, "dmarc": "pass"}]},
    }
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    conn = _test_connection(autocommit=False)
    try:
        count = publish_all_views(int_db_config, conn, TOOLBOX_URL, logging.getLogger("it-outlook"))
    finally:
        conn.close()

    assert count == 2
    jobs = {j["reference_id"]: j for j in fetch_all_jobs()}
    assert jobs["m-dev"]["agent_view_id"] == dev_id
    assert jobs["m-dev"]["requester_email"] == "sklep@mycompanystudio.com"
    assert jobs["m-dev"]["requester_trust"] == "domain"
    assert jobs["m-ops"]["agent_view_id"] == ops_id


@respx.mock
def test_spoof_and_stranger_blocked_per_view(int_db_config, two_views, caplog):
    dev_id, ops_id = two_views
    by_view = {
        dev_id: {"mailbox": "dev@example.com", "messages": [
            {"id": "m-pass", "from": {"address": "sklep@mycompanystudio.com"}, "dmarc": "pass"},
            {"id": "m-stranger", "from": {"address": "stranger@elsewhere.com"}, "dmarc": "pass"},
            {"id": "m-spoof", "from": {"address": "sklep@mycompanystudio.com"}, "dmarc": "fail"},
        ]},
        ops_id: {"mailbox": "ops@example.com", "messages": []},
    }
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    conn = _test_connection(autocommit=False)
    try:
        with caplog.at_level(logging.ERROR):
            count = publish_all_views(int_db_config, conn, TOOLBOX_URL, logging.getLogger("it-outlook"))
    finally:
        conn.close()

    assert count == 1
    jobs = fetch_all_jobs()
    assert [j["reference_id"] for j in jobs] == ["m-pass"]
    assert any("SECURITY_BREACH" in r.getMessage() for r in caplog.records)


@respx.mock
def test_shared_mailbox_routes_by_sender(int_db_config, two_views, routing_registered):
    """A UPN shared by >=2 views is ROUTED mode: polled once (by the lowest-id poll_owner) and each
    message routed to a member view by matching the normalized sender against outlook_sender
    ingress bindings (regex, priority). 'Lowest id wins' is GONE."""
    dev_id, ops_id = two_views  # dev_id < ops_id (insertion order)
    conn = _test_connection(autocommit=True)
    try:
        for av_id in (dev_id, ops_id):
            scoped_config_set(conn, "outlook/outlook_mailbox_user_id", "shared@example.com",
                              scope=Scope.AGENT_VIEW, scope_id=av_id)
        # sklep@... -> dev (exact), any @mycompany.com -> ops (domain fallback)
        bind_identity(conn, "outlook_sender", r"sklep@mycompanystudio\.com", dev_id, priority=10)
        bind_identity(conn, "outlook_sender", r"[^@]+@mycompany\.com", ops_id, priority=0)
    finally:
        conn.close()

    assert is_regex_identity_type("outlook_sender")
    shared = {"mailbox": "shared@example.com", "deltaLink": "L-shared", "messages": [
        {"id": "m-dev", "from": {"address": "sklep@mycompanystudio.com"}, "dmarc": "pass"},
        {"id": "m-ops", "from": {"address": "ops@mycompany.com"}, "dmarc": "pass"},
    ]}
    # both views resolve to the shared UPN, so only the poll_owner (dev_id, lowest) is polled
    respx.post(DELTA_URL).mock(side_effect=_delta_stub({dev_id: shared, ops_id: shared}))

    conn = _test_connection(autocommit=False)
    try:
        count = publish_all_views(int_db_config, conn, TOOLBOX_URL, logging.getLogger("it-outlook"))
    finally:
        conn.close()

    assert count == 2
    jobs = {j["reference_id"]: j for j in fetch_all_jobs()}
    assert jobs["m-dev"]["agent_view_id"] == dev_id   # sklep@ -> dev (exact binding)
    assert jobs["m-ops"]["agent_view_id"] == ops_id   # ops@mycompany.com -> ops (domain fallback)
    # the shared cursor advanced once, keyed by the normalized shared UPN
    rconn = _test_connection(autocommit=False)
    try:
        assert load_cursors(rconn) == {"shared@example.com": "L-shared"}
    finally:
        rconn.close()


@respx.mock
def test_four_views_routed_and_direct_coexist(int_db_config, routing_registered):
    """PRD §30 end-to-end: THREE views share one mailbox (ROUTED by sender), ONE solo view is
    DIRECT — in a single poll pass. Asserts the full routing table: each matching sender lands a
    job on ITS bound view; a non-matching (but allow-listed) sender yields NO job while the cursor
    still advances; the solo mailbox publishes to its own view without routing; and a routed job
    carries the TARGET view's scheduling priority (not the lowest-id poll_owner's default)."""
    conn = _test_connection(autocommit=True)
    av: dict[str, int] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO workspace (code, label) VALUES ('ws-outlook-4v', 'outlook 4v')")
            ws_id = cur.lastrowid
            for code in ("av4-a", "av4-b", "av4-c", "av4-solo"):
                cur.execute(
                    "INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, %s, %s)",
                    (ws_id, code, code),
                )
                av[code] = cur.lastrowid
        # The three shared-mailbox members MUST carry identical *activation* policy (default via
        # fallback here), but may now hold DIFFERENT allowed_senders — each persona's own per-view
        # safety-net, evaluated after routing. The auto-derived UNION admits any member-trusted
        # sender; the routed-to view's own list refines. av4-a also trusts *@stranger.com so the
        # allow-listed-but-unbound m-x is admitted then dropped at routing (unroutable).
        per_view_allowed = {
            "av4-a": "*@partner.com, *@stranger.com",
            "av4-b": "*@client.com",
            "av4-c": "bob@vendor.com",
        }
        for code in ("av4-a", "av4-b", "av4-c"):
            scoped_config_set(conn, "outlook/enabled", "1", scope=Scope.AGENT_VIEW, scope_id=av[code])
            scoped_config_set(conn, "outlook/allowed_senders", per_view_allowed[code], scope=Scope.AGENT_VIEW, scope_id=av[code])
            scoped_config_set(conn, "outlook/outlook_mailbox_user_id", "agents@company.com", scope=Scope.AGENT_VIEW, scope_id=av[code])
        # Solo view: its OWN mailbox → direct mode.
        scoped_config_set(conn, "outlook/enabled", "1", scope=Scope.AGENT_VIEW, scope_id=av["av4-solo"])
        scoped_config_set(conn, "outlook/allowed_senders", "*@company.com", scope=Scope.AGENT_VIEW, scope_id=av["av4-solo"])
        scoped_config_set(conn, "outlook/outlook_mailbox_user_id", "solo@company.com", scope=Scope.AGENT_VIEW, scope_id=av["av4-solo"])
        # Target view B gets a distinct scheduling priority so we can prove the job priority comes
        # from the TARGET, not the poll_owner (av4-a, lowest id, default priority).
        scoped_config_set(conn, "agent_view/scheduling/priority", "7", scope=Scope.AGENT_VIEW, scope_id=av["av4-b"])
        # Three sender→view bindings on the shared mailbox.
        bind_identity(conn, "outlook_sender", r"alice@partner\.com", av["av4-a"], priority=10)
        bind_identity(conn, "outlook_sender", r"[^@]+@client\.com", av["av4-b"], priority=5)
        bind_identity(conn, "outlook_sender", r"bob@vendor\.com", av["av4-c"], priority=5)
    finally:
        conn.close()

    shared = {"mailbox": "agents@company.com", "deltaLink": "L-agents", "messages": [
        {"id": "m-a", "from": {"address": "alice@partner.com"}, "dmarc": "pass"},
        {"id": "m-b", "from": {"address": "user@client.com"}, "dmarc": "pass"},
        {"id": "m-c", "from": {"address": "bob@vendor.com"}, "dmarc": "pass"},
        {"id": "m-x", "from": {"address": "nobody@stranger.com"}, "dmarc": "pass"},  # allow-listed, no binding
    ]}
    solo = {"mailbox": "solo@company.com", "deltaLink": "L-solo", "messages": [
        {"id": "m-d", "from": {"address": "boss@company.com"}, "dmarc": "pass"},
    ]}
    # Only the poll_owner of each group is polled; map every member so whichever id is posted works.
    by_view = {av["av4-a"]: shared, av["av4-b"]: shared, av["av4-c"]: shared, av["av4-solo"]: solo}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    try:
        pconn = _test_connection(autocommit=False)
        try:
            count = publish_all_views(int_db_config, pconn, TOOLBOX_URL, logging.getLogger("it-outlook-4v"))
        finally:
            pconn.close()

        jobs = {j["reference_id"]: j for j in fetch_all_jobs()}
        # Routed: each matching sender → its bound view (poll_owner av4-a is NOT the target for b/c).
        assert jobs["m-a"]["agent_view_id"] == av["av4-a"]   # alice@partner.com (exact)
        assert jobs["m-b"]["agent_view_id"] == av["av4-b"]   # *@client.com (domain)
        assert jobs["m-c"]["agent_view_id"] == av["av4-c"]   # bob@vendor.com
        # Non-matching (but allow-listed) sender → NO job.
        assert "m-x" not in jobs
        # Solo mailbox → its own view, DIRECT mode.
        assert jobs["m-d"]["agent_view_id"] == av["av4-solo"]
        assert count == 4
        # Routed job carries the TARGET view's scheduling priority (B=7), not the poll_owner's.
        assert jobs["m-b"]["priority"] == 7
        # Both mailboxes' cursors advanced once — the deterministic no-match on m-x does NOT hold.
        rconn = _test_connection(autocommit=False)
        try:
            assert load_cursors(rconn) == {"agents@company.com": "L-agents", "solo@company.com": "L-solo"}
        finally:
            rconn.close()
    finally:
        cconn = _test_connection(autocommit=True)
        try:
            with cconn.cursor() as cur:
                for av_id in av.values():
                    cur.execute("DELETE FROM core_config_data WHERE scope = 'agent_view' AND scope_id = %s", (av_id,))
                cur.execute("DELETE FROM workspace WHERE code = 'ws-outlook-4v'")
        finally:
            cconn.close()


@respx.mock
def test_junk_starvation_gone_valid_mail_published_behind_junk(int_db_config, two_views):
    """ACC: >= poll_top non-allow-listed unread, OLDER than one allow-listed DMARC-pass mail. The delta
    handler pages the whole set (no fixed-window truncation) so the valid mail publishes."""
    dev_id, _ = two_views
    junk = [{"id": f"junk-{i}", "from": {"address": f"spam{i}@nowhere.com"}, "dmarc": "pass"} for i in range(15)]
    valid = {"id": "valid-1", "from": {"address": "sklep@mycompanystudio.com"}, "dmarc": "pass"}
    by_view = {dev_id: {"mailbox": "dev@example.com", "messages": [*junk, valid]}}  # valid is LAST (newest)
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    conn = _test_connection(autocommit=False)
    try:
        count = publish_all_views(int_db_config, conn, TOOLBOX_URL,
                                  logging.getLogger("it-outlook"), agent_view_code="av-outlook-dev")
    finally:
        conn.close()
    assert count == 1
    assert [j["reference_id"] for j in fetch_all_jobs()] == ["valid-1"]


@respx.mock
def test_in_flight_published_but_unread_do_not_block_new_valid_mail(int_db_config, two_views):
    """ACC: >= poll_top already-published-but-still-unread messages must not block a newer valid one."""
    dev_id, _ = two_views
    inflight = [{"id": f"wip-{i}", "from": {"address": "sklep@mycompanystudio.com"}, "dmarc": "pass"} for i in range(12)]
    new_valid = {"id": "new-valid", "from": {"address": "ops@mycompany.com"}, "dmarc": "pass"}
    by_view = {dev_id: {"mailbox": "dev@example.com", "messages": [*inflight, new_valid]}}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    conn = _test_connection(autocommit=False)
    try:
        publish_all_views(int_db_config, conn, TOOLBOX_URL,
                          logging.getLogger("it-outlook"), agent_view_code="av-outlook-dev")
    finally:
        conn.close()
    refs = {j["reference_id"] for j in fetch_all_jobs()}
    assert "new-valid" in refs  # the newer valid mail was reached, not starved


@respx.mock
def test_cursor_written_only_after_publish_keyed_by_mailbox(int_db_config, two_views):
    """ACC: outlook_poll_cursor is written, keyed by normalized mailbox UPN, only after publishing."""
    dev_id, _ = two_views
    link = "https://graph.microsoft.com/v1.0/users/dev@example.com/mailFolders/Inbox/messages/delta?$deltatoken=ABC"
    by_view = {dev_id: {"mailbox": "DEV@Example.com", "deltaLink": link,
                        "messages": [{"id": "c1", "from": {"address": "sklep@mycompanystudio.com"}, "dmarc": "pass"}]}}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    conn = _test_connection(autocommit=False)
    try:
        publish_all_views(int_db_config, conn, TOOLBOX_URL,
                          logging.getLogger("it-outlook"), agent_view_code="av-outlook-dev")
    finally:
        conn.close()

    rconn = _test_connection(autocommit=False)
    try:
        assert load_cursors(rconn) == {"dev@example.com": link}  # normalized key, full deltaLink persisted
    finally:
        rconn.close()


@respx.mock
def test_long_message_id_round_trips_untruncated(int_db_config, two_views):
    """Regression for migration 027: a >255-char Graph id + 'outlook:mail:' prefix must persist
    untruncated. On the old VARCHAR(255) column, INSERT IGNORE under strict sql_mode silently
    truncated it, colliding distinct emails and dropping the second as a phantom duplicate."""
    dev_id, _ = two_views
    long_id = "AAMk" + "B" * 260  # 264 chars, well over 255
    by_view = {dev_id: {"mailbox": "dev@example.com", "messages": [
        {"id": long_id, "subject": "Long id test",
         "from": {"address": "sklep@mycompanystudio.com"}, "dmarc": "pass"}]}}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    conn = _test_connection(autocommit=False)
    try:
        count = publish_all_views(int_db_config, conn, TOOLBOX_URL,
                                  logging.getLogger("it-outlook"), agent_view_code="av-outlook-dev")
    finally:
        conn.close()

    assert count == 1
    job = fetch_all_jobs()[0]
    assert job["idempotency_key"] == f"outlook:mail:{long_id}"  # full key, no truncation
    assert job["reference_id"].rsplit("::", 1)[-1] == long_id    # message_id survives at the tail


@respx.mock
def test_subject_becomes_readable_compound_reference_id(int_db_config, two_views):
    """A polled subject becomes a slug prefix on reference_id (readable in logs/admin) while the
    bare message_id stays recoverable at the tail and the idempotency_key stays bare."""
    dev_id, _ = two_views
    by_view = {dev_id: {"mailbox": "dev@example.com", "messages": [
        {"id": "AAMkXYZ", "subject": "Zażółć: Raport & wnioski",
         "from": {"address": "sklep@mycompanystudio.com"}, "dmarc": "pass"}]}}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    conn = _test_connection(autocommit=False)
    try:
        publish_all_views(int_db_config, conn, TOOLBOX_URL,
                          logging.getLogger("it-outlook"), agent_view_code="av-outlook-dev")
    finally:
        conn.close()

    job = fetch_all_jobs()[0]
    assert job["reference_id"] == "zazolc-raport-wnioski::AAMkXYZ"
    assert job["reference_id"].rsplit("::", 1)[-1] == "AAMkXYZ"
    assert job["idempotency_key"] == "outlook:mail:AAMkXYZ"  # bare, dedup keys on message_id only


@respx.mock
def test_replay_same_delta_creates_no_duplicate_jobs(int_db_config, two_views):
    """ACC: resync / held-cursor replays must not duplicate jobs (idempotency_key holds)."""
    dev_id, _ = two_views
    by_view = {dev_id: {"mailbox": "dev@example.com", "deltaLink": "L1",
                        "messages": [{"id": "dup-1", "from": {"address": "sklep@mycompanystudio.com"}, "dmarc": "pass"}]}}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    for _ in range(2):  # poll twice with the SAME message id
        conn = _test_connection(autocommit=False)
        try:
            publish_all_views(int_db_config, conn, TOOLBOX_URL,
                              logging.getLogger("it-outlook"), agent_view_code="av-outlook-dev")
        finally:
            conn.close()

    assert len([j for j in fetch_all_jobs() if j["reference_id"] == "dup-1"]) == 1


# ---- Activation gate (Workstream 3/4): direct / mention / loop-marker suppression ----

def _run_dev(int_db_config):
    conn = _test_connection(autocommit=False)
    try:
        return publish_all_views(int_db_config, conn, TOOLBOX_URL,
                                 logging.getLogger("it-outlook"), agent_view_code="av-outlook-dev")
    finally:
        conn.close()


@respx.mock
def test_direct_addressed_creates_job(int_db_config, two_views):
    dev_id, _ = two_views
    by_view = {dev_id: {"mailbox": "dev@example.com", "messages": [
        {"id": "direct-1", "from": {"address": "sklep@mycompanystudio.com"}, "dmarc": "pass",
         "to": [{"address": "dev@example.com"}], "cc": []}]}}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    assert _run_dev(int_db_config) == 1
    assert [j["reference_id"] for j in fetch_all_jobs()] == ["direct-1"]


@respx.mock
def test_cc_only_no_mention_no_job_but_cursor_advances(int_db_config, two_views):
    """A mail where the mailbox is only a cc (a human is the addressee) and no summon token appears
    must NOT create a job — yet the cursor still advances (a policy decision never holds)."""
    dev_id, _ = two_views
    by_view = {dev_id: {"mailbox": "dev@example.com", "deltaLink": "L-CC", "messages": [
        {"id": "cc-only", "from": {"address": "sklep@mycompanystudio.com"}, "dmarc": "pass",
         "to": [{"address": "colleague@example.com"}], "cc": [{"address": "dev@example.com"}],
         "bodyPreview": "no summon here", "subject": "zwykła sprawa"}]}}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    assert _run_dev(int_db_config) == 0
    assert not fetch_all_jobs()
    rconn = _test_connection(autocommit=False)
    try:
        assert load_cursors(rconn) == {"dev@example.com": "L-CC"}  # advanced despite no job
    finally:
        rconn.close()


@respx.mock
def test_summon_token_creates_job_even_when_not_directly_addressed(int_db_config, two_views):
    dev_id, _ = two_views
    by_view = {dev_id: {"mailbox": "dev@example.com", "messages": [
        {"id": "mention-1", "from": {"address": "sklep@mycompanystudio.com"}, "dmarc": "pass",
         "to": [{"address": "colleague@example.com"}], "cc": [{"address": "team@example.com"}],
         "subject": "Re: raport", "bodyPreview": "cześć @agento, przygotuj podsumowanie"}]}}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    assert _run_dev(int_db_config) == 1
    assert [j["reference_id"].rsplit("::", 1)[-1] for j in fetch_all_jobs()] == ["mention-1"]


@respx.mock
def test_agent_authored_inbound_with_humans_default_no_job(int_db_config, two_views):
    """Regression F2: an agent-authored inbound addressed straight at the mailbox (would be direct),
    with humans also present, must create NO job under the default config (hard loop suppression)."""
    dev_id, _ = two_views
    by_view = {dev_id: {"mailbox": "dev@example.com", "messages": [
        {"id": "bot-1", "from": {"address": "ops@mycompany.com"}, "dmarc": "pass",
         "to": [{"address": "dev@example.com"}, {"address": "human@example.com"}], "cc": [],
         "agent_authored": True, "subject": "@agento kontynuuj", "bodyPreview": "@agento kontynuuj"}]}}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    assert _run_dev(int_db_config) == 0
    assert not fetch_all_jobs()


@respx.mock
def test_allow_bot_collaboration_lets_agent_authored_through(int_db_config, two_views):
    dev_id, _ = two_views
    conn = _test_connection(autocommit=True)
    try:
        scoped_config_set(conn, "outlook/allow_bot_collaboration", "1",
                          scope=Scope.AGENT_VIEW, scope_id=dev_id)
    finally:
        conn.close()

    by_view = {dev_id: {"mailbox": "dev@example.com", "messages": [
        {"id": "bot-collab-1", "from": {"address": "ops@mycompany.com"}, "dmarc": "pass",
         "to": [{"address": "dev@example.com"}], "cc": [], "agent_authored": True}]}}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    assert _run_dev(int_db_config) == 1
    assert [j["reference_id"] for j in fetch_all_jobs()] == ["bot-collab-1"]


# --- Route-first per-view authorization (real MySQL + real router/config) ----------------------


@respx.mock
def test_two_views_share_upn_with_different_allowlists_no_stall(int_db_config, two_views, routing_registered):
    """Acceptance #1 + #2: two views share one UPN with DIFFERENT allowed_senders (previously a
    divergence STALL). Each sender routes to its own view, gated by THAT view's list; both publish."""
    dev_id, ops_id = two_views
    conn = _test_connection(autocommit=True)
    try:
        for av_id in (dev_id, ops_id):
            scoped_config_set(conn, "outlook/outlook_mailbox_user_id", "shared@example.com",
                              scope=Scope.AGENT_VIEW, scope_id=av_id)
        scoped_config_set(conn, "outlook/allowed_senders", "*@dev.com", scope=Scope.AGENT_VIEW, scope_id=dev_id)
        scoped_config_set(conn, "outlook/allowed_senders", "*@ops.com", scope=Scope.AGENT_VIEW, scope_id=ops_id)
        bind_identity(conn, "outlook_sender", r"[^@]+@dev\.com", dev_id, priority=10)
        bind_identity(conn, "outlook_sender", r"[^@]+@ops\.com", ops_id, priority=10)
    finally:
        conn.close()

    shared = {"mailbox": "shared@example.com", "deltaLink": "L-shared", "messages": [
        {"id": "m-dev", "from": {"address": "a@dev.com"}, "dmarc": "pass"},
        {"id": "m-ops", "from": {"address": "b@ops.com"}, "dmarc": "pass"},
    ]}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub({dev_id: shared, ops_id: shared}))

    conn = _test_connection(autocommit=False)
    try:
        count = publish_all_views(int_db_config, conn, TOOLBOX_URL, logging.getLogger("it-outlook"))
    finally:
        conn.close()

    assert count == 2
    jobs = {j["reference_id"]: j for j in fetch_all_jobs()}
    assert jobs["m-dev"]["agent_view_id"] == dev_id
    assert jobs["m-ops"]["agent_view_id"] == ops_id
    rconn = _test_connection(autocommit=False)
    try:
        assert load_cursors(rconn) == {"shared@example.com": "L-shared"}
    finally:
        rconn.close()


@respx.mock
def test_routed_but_target_view_allowlist_rejects_drops_fail_closed(int_db_config, two_views, routing_registered):
    """A sender passes the UNION (dev trusts it) but its binding routes it to OPS, whose own list
    rejects it -> no job, cursor still advances (deterministic drop, not a hold)."""
    dev_id, ops_id = two_views
    conn = _test_connection(autocommit=True)
    try:
        for av_id in (dev_id, ops_id):
            scoped_config_set(conn, "outlook/outlook_mailbox_user_id", "shared@example.com",
                              scope=Scope.AGENT_VIEW, scope_id=av_id)
        scoped_config_set(conn, "outlook/allowed_senders", "*@dev.com", scope=Scope.AGENT_VIEW, scope_id=dev_id)
        scoped_config_set(conn, "outlook/allowed_senders", "*@ops.com", scope=Scope.AGENT_VIEW, scope_id=ops_id)
        # route a dev-domain sender to OPS on purpose (OPS's own list does NOT include *@dev.com)
        bind_identity(conn, "outlook_sender", r"[^@]+@dev\.com", ops_id, priority=10)
    finally:
        conn.close()

    shared = {"mailbox": "shared@example.com", "deltaLink": "L-shared", "messages": [
        {"id": "m1", "from": {"address": "a@dev.com"}, "dmarc": "pass"}]}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub({dev_id: shared, ops_id: shared}))

    conn = _test_connection(autocommit=False)
    try:
        count = publish_all_views(int_db_config, conn, TOOLBOX_URL, logging.getLogger("it-outlook"))
    finally:
        conn.close()

    assert count == 0
    assert not fetch_all_jobs()
    rconn = _test_connection(autocommit=False)
    try:
        assert load_cursors(rconn) == {"shared@example.com": "L-shared"}  # advanced (no hold)
    finally:
        rconn.close()


@respx.mock
def test_unset_routed_view_allowlist_inherits_default_not_deny_all(int_db_config, two_views, routing_registered):
    """ROUTED mode: a routed-to view with NO agent_view-scoped allowed_senders row inherits the
    DEFAULT-scope value through the post-route per-view refinement's agent_view→workspace→default
    chain (it does NOT silently black-hole). Both views stay on one shared UPN so routing runs."""
    dev_id, ops_id = two_views
    conn = _test_connection(autocommit=True)
    try:
        for av_id in (dev_id, ops_id):
            scoped_config_set(conn, "outlook/outlook_mailbox_user_id", "shared@example.com",
                              scope=Scope.AGENT_VIEW, scope_id=av_id)
        # dev has NO per-view allow-list row -> must inherit the DEFAULT below; ops keeps its own list
        with conn.cursor() as cur:
            cur.execute("DELETE FROM core_config_data WHERE scope='agent_view' AND scope_id=%s "
                        "AND path='outlook/allowed_senders'", (dev_id,))
        scoped_config_set(conn, "outlook/allowed_senders", "*@ops.com", scope=Scope.AGENT_VIEW, scope_id=ops_id)
        scoped_config_set(conn, "outlook/allowed_senders", "*@dev.com", scope=Scope.DEFAULT, scope_id=0)
        bind_identity(conn, "outlook_sender", r"[^@]+@dev\.com", dev_id, priority=10)
    finally:
        conn.close()

    shared = {"mailbox": "shared@example.com", "deltaLink": "L-shared", "messages": [
        {"id": "m-inherit", "from": {"address": "a@dev.com"}, "dmarc": "pass"}]}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub({dev_id: shared, ops_id: shared}))

    try:
        conn = _test_connection(autocommit=False)
        try:
            count = publish_all_views(int_db_config, conn, TOOLBOX_URL, logging.getLogger("it-outlook"))
        finally:
            conn.close()
        # a@dev.com: union (dev's inherited *@dev.com plus ops's *@ops.com) admits -> routes to dev ->
        # dev's per-view refinement resolves the DEFAULT *@dev.com -> matches -> routed publication.
        assert count == 1
        jobs = {j["reference_id"]: j for j in fetch_all_jobs()}
        assert jobs["m-inherit"]["agent_view_id"] == dev_id
    finally:
        cconn = _test_connection(autocommit=True)
        try:
            with cconn.cursor() as cur:
                cur.execute("DELETE FROM core_config_data WHERE scope='default' AND scope_id=0 "
                            "AND path='outlook/allowed_senders'")
        finally:
            cconn.close()


@respx.mock
def test_single_view_direct_mode_zero_bindings_still_publishes(int_db_config, two_views):
    """Acceptance #4 regression: a single view on a UPN with ZERO ingress bindings publishes in
    DIRECT mode (no routing required)."""
    dev_id, ops_id = two_views  # each has its own mailbox -> both direct; assert dev publishes
    by_view = {
        dev_id: {"mailbox": "dev@example.com", "deltaLink": "L-dev", "messages": [
            {"id": "m-direct", "from": {"address": "sklep@mycompanystudio.com"}, "dmarc": "pass"}]},
        ops_id: {"mailbox": "ops@example.com", "deltaLink": "L-ops", "messages": []},
    }
    respx.post(DELTA_URL).mock(side_effect=_delta_stub(by_view))

    conn = _test_connection(autocommit=False)
    try:
        count = publish_all_views(int_db_config, conn, TOOLBOX_URL, logging.getLogger("it-outlook"))
    finally:
        conn.close()

    assert count == 1
    jobs = {j["reference_id"]: j for j in fetch_all_jobs()}
    assert jobs["m-direct"]["agent_view_id"] == dev_id


@respx.mock
def test_dmarc_still_enforced_and_breach_scoped_to_union(int_db_config, two_views, routing_registered, caplog):
    """Acceptance #3: DMARC is still enforced for all admitted mail; the SECURITY_BREACH alert is
    scoped to UNION-trusted senders (a spoof from a sender no view trusts is silently dropped)."""
    dev_id, ops_id = two_views
    conn = _test_connection(autocommit=True)
    try:
        for av_id in (dev_id, ops_id):
            scoped_config_set(conn, "outlook/outlook_mailbox_user_id", "shared@example.com",
                              scope=Scope.AGENT_VIEW, scope_id=av_id)
        scoped_config_set(conn, "outlook/allowed_senders", "*@dev.com", scope=Scope.AGENT_VIEW, scope_id=dev_id)
        scoped_config_set(conn, "outlook/allowed_senders", "*@ops.com", scope=Scope.AGENT_VIEW, scope_id=ops_id)
        bind_identity(conn, "outlook_sender", r"[^@]+@dev\.com", dev_id, priority=10)
    finally:
        conn.close()

    shared = {"mailbox": "shared@example.com", "deltaLink": "L-shared", "messages": [
        {"id": "m-spoof-in", "from": {"address": "a@dev.com"}, "dmarc": "fail"},   # union-trusted -> breach
        {"id": "m-spoof-out", "from": {"address": "c@evil.com"}, "dmarc": "fail"},  # not trusted -> silent
    ]}
    respx.post(DELTA_URL).mock(side_effect=_delta_stub({dev_id: shared, ops_id: shared}))

    conn = _test_connection(autocommit=False)
    try:
        with caplog.at_level(logging.ERROR):
            count = publish_all_views(int_db_config, conn, TOOLBOX_URL, logging.getLogger("it-outlook"))
    finally:
        conn.close()

    assert count == 0
    assert not fetch_all_jobs()
    breach_logs = [r for r in caplog.records if "SECURITY_BREACH" in r.getMessage()]
    assert len(breach_logs) == 1  # only the union-trusted spoof alerts
