"""Integration: the refresh lease stops ten concurrent workers from replaying the SAME
single-use refresh token (real MySQL — the lease is enforced by SQL predicates and DB-side
clock arithmetic, so a mocked cursor could not prove any of this).

The named timeline test reproduces the k3-agento incident of 2026-07-15 and fails without
the lease.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agento.framework.agent_manager.credential_store import (
    RefreshLease,
    get_credential,
    lease_owner_for_job,
    register_credential,
    release_credential_lease,
    renew_credential_leases,
    select_credential,
    update_refreshed_credentials,
)
from agento.framework.agent_manager.errors import (
    CredentialLeasedError,
    TransientAuthError,
)
from agento.framework.agent_manager.models import CredentialRecord, encrypt_credentials
from agento.framework.consumer import Consumer

from .conftest import _test_connection

_LEASE_TTL = 300


def _seed(
    label: str,
    *,
    priority: int,
    refresh_token: str | None,
    type: str = "oauth",
    scope: str = "claude",
) -> int:
    """Insert an enabled healthy credential. ``refresh_token=None`` mimics an API-key row
    (no rotating secret), which must never be made exclusive."""
    creds: dict = {"subscription_key": f"sk-{label}"}
    if refresh_token is not None:
        creds["refresh_token"] = refresh_token
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO credential
                    (scope, agent_type, type, label, credentials, enabled, status, priority)
                VALUES (%s, %s, %s, %s, %s, TRUE, 'ok', %s)
                """,
                (scope, scope, type, label, encrypt_credentials(creds), priority),
            )
            return cur.lastrowid
    finally:
        conn.close()


def _row(token_id: int) -> dict:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM credential WHERE id = %s", (token_id,))
            return cur.fetchone()
    finally:
        conn.close()


def _set_leased_until(token_id: int, delta_seconds: int, owner: str) -> None:
    """Force a lease deadline relative to the DB clock (never the host's)."""
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE credential SET lease_owner = %s, "
                "leased_until = UTC_TIMESTAMP() + INTERVAL %s SECOND WHERE id = %s",
                (owner, delta_seconds, token_id),
            )
    finally:
        conn.close()


def _lease(owner: str, *, should_lease=lambda _t: True) -> RefreshLease:
    return RefreshLease(owner=owner, ttl_seconds=_LEASE_TTL, should_lease=should_lease)


def _claim(owner: str | None, *, should_lease=lambda _t: True) -> CredentialRecord | None:
    """One claim on its own connection, exactly like one worker."""
    conn = _test_connection()
    try:
        return select_credential(
            conn,
            "claude",
            _lease(owner, should_lease=should_lease) if owner else None,
        )
    finally:
        conn.close()


def _rotating(credential: CredentialRecord) -> bool:
    """The real policy's shape: only a credential with something to rotate is exclusive."""
    return bool((credential.credentials or {}).get("refresh_token"))


@pytest.fixture(autouse=True)
def _clean_tokens():
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM credential")
    finally:
        conn.close()
    yield


def test_2026_07_15_two_job_timeline_does_not_replay_a_spent_refresh_token():
    """The incident, end to end. Fails without the lease: job 110416 is handed the same
    spent R0 that job 110409 is about to rotate, 401s, and quarantines the subscription."""
    oauth_id = _seed("prod-1", priority=0, refresh_token="R0")
    api_id = _seed("api-fallback", priority=99, refresh_token=None, type="anthropic_api_key")

    # 16:11 — job 110409 claims the subscription and leases it (near-expiry credential).
    first = _claim(lease_owner_for_job(110409, 1), should_lease=_rotating)
    assert first.id == oauth_id
    assert first.lease_owner == "job-110409-attempt-1"
    assert first.credentials["refresh_token"] == "R0"

    # 16:13 — job 110416 must NOT get the spent R0; it overflows onto the paid API key.
    second = _claim(lease_owner_for_job(110416, 1), should_lease=_rotating)
    assert second.id == api_id
    assert (second.credentials or {}).get("refresh_token") != "R0"

    # 16:38 — 110409 finishes: its rotation is captured and the lease released.
    conn = _test_connection()
    try:
        update_refreshed_credentials(
            conn, oauth_id, {"subscription_key": "sk-new", "refresh_token": "R1"}
        )
        assert release_credential_lease(conn, oauth_id, "job-110409-attempt-1") is True
        conn.commit()
    finally:
        conn.close()

    # 16:39 — the subscription is selectable again, with the ROTATED token and no lease.
    third = _claim(lease_owner_for_job(110420, 1), should_lease=_rotating)
    assert third.id == oauth_id
    assert third.credentials["refresh_token"] == "R1"
    assert third.status.value == "ok"


def test_unleased_selection_hands_the_same_spent_token_to_a_second_job():
    """The bug itself, kept as documentation: with no lease requested (the pre-fix code
    path, and every caller that spawns no CLI) both claimants get the same credential."""
    oauth_id = _seed("prod-1", priority=0, refresh_token="R0")
    _seed("api-fallback", priority=99, refresh_token=None, type="anthropic_api_key")

    first = _claim(None)
    second = _claim(None)

    assert first.id == second.id == oauth_id
    assert first.credentials["refresh_token"] == second.credentials["refresh_token"] == "R0"


def test_ten_barriered_workers_take_exactly_one_lease():
    """AGENTO_CONSUMER_MAX_WORKERS=10, released simultaneously: exactly one may hold the
    refresh-imminent row. Without the lease all ten do."""
    oauth_id = _seed("prod-1", priority=0, refresh_token="R0")
    api_id = _seed("api-fallback", priority=99, refresh_token=None, type="anthropic_api_key")

    barrier = threading.Barrier(10)
    claimed: list[CredentialRecord] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        barrier.wait()
        token = _claim(lease_owner_for_job(n, 1), should_lease=_rotating)
        with lock:
            claimed.append(token)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 10
    assert sum(1 for t in claimed if t.id == oauth_id) == 1
    assert sum(1 for t in claimed if t.id == api_id) == 9


def test_fresh_credential_is_shared_by_all_ten_workers():
    """The fix must NOT serialize the pool: a credential nowhere near expiry stays shared,
    so 10-way concurrency on one licence is unchanged."""
    oauth_id = _seed("prod-1", priority=0, refresh_token="R0")

    for n in range(10):
        token = _claim(lease_owner_for_job(n, 1), should_lease=lambda _t: False)
        assert token.id == oauth_id
        assert token.lease_owner is None

    assert _row(oauth_id)["leased_until"] is None


def test_expired_lease_is_selectable_again_and_a_stale_holder_cannot_free_the_new_lease():
    """SIGKILL recovery with no reaper: nobody renews, so the deadline simply passes."""
    oauth_id = _seed("prod-1", priority=0, refresh_token="R0")
    _set_leased_until(oauth_id, -1, "job-1-attempt-1")

    taken = _claim(lease_owner_for_job(2, 1), should_lease=_rotating)
    assert taken.id == oauth_id
    assert taken.lease_owner == "job-2-attempt-1"

    conn = _test_connection()
    try:
        # The dead consumer's late cleanup must not free the lease its successor holds.
        assert release_credential_lease(conn, oauth_id, "job-1-attempt-1") is False
        conn.commit()
    finally:
        conn.close()
    assert _row(oauth_id)["lease_owner"] == "job-2-attempt-1"


def test_attempt_1_cleanup_cannot_release_attempt_2_lease():
    """The ABA guard: both attempts of one job would share a `job-{id}` owner string."""
    oauth_id = _seed("prod-1", priority=0, refresh_token="R0")
    _set_leased_until(oauth_id, _LEASE_TTL, lease_owner_for_job(7, 2))

    conn = _test_connection()
    try:
        assert release_credential_lease(conn, oauth_id, lease_owner_for_job(7, 1)) is False
        conn.commit()
    finally:
        conn.close()
    assert _row(oauth_id)["lease_owner"] == "job-7-attempt-2"


def test_a_live_job_keeps_its_lease_past_the_lease_ttl():
    """Renewal proof, with a deliberately tiny TTL instead of a multi-minute wall-clock
    wait: `leased_until` moves forward and the row stays unselectable."""
    oauth_id = _seed("prod-1", priority=0, refresh_token="R0")
    owner = lease_owner_for_job(9, 1)
    _set_leased_until(oauth_id, 1, owner)  # about to expire

    conn = _test_connection()
    try:
        assert renew_credential_leases(conn, [owner], _LEASE_TTL) == 1
        conn.commit()
    finally:
        conn.close()

    row = _row(oauth_id)
    now_naive_utc = datetime.now(UTC).replace(tzinfo=None)
    assert row["leased_until"] > now_naive_utc + timedelta(seconds=_LEASE_TTL - 60)
    assert _claim(lease_owner_for_job(10, 1), should_lease=_rotating) is None


def test_a_second_consumer_renews_only_its_own_leases():
    """Two consumers, one credential table: renewal is keyed by owner, never by scope."""
    mine = _seed("prod-1", priority=0, refresh_token="R0")
    theirs = _seed("prod-2", priority=1, refresh_token="R0b")
    _set_leased_until(mine, 5, "job-1-attempt-1")
    _set_leased_until(theirs, 5, "job-2-attempt-1")

    conn = _test_connection()
    try:
        assert renew_credential_leases(conn, ["job-1-attempt-1"], _LEASE_TTL) == 1
        conn.commit()
    finally:
        conn.close()

    assert _row(mine)["leased_until"] > _row(theirs)["leased_until"]


def test_two_connections_cannot_double_lease():
    """The claim itself is serialized by the row lock, so the second claimant sees the
    lease the first committed rather than racing past it."""
    oauth_id = _seed("prod-1", priority=0, refresh_token="R0")

    first = _claim(lease_owner_for_job(1, 1), should_lease=_rotating)
    second = _claim(lease_owner_for_job(2, 1), should_lease=_rotating)

    assert first.id == oauth_id
    assert second is None
    assert _row(oauth_id)["lease_owner"] == "job-1-attempt-1"


def test_an_expired_lease_is_cleared_when_the_row_is_selected_unleased():
    """Otherwise `credential:list` reports a long-dead holder and the "rotated without a lease"
    detector — the only instrument for the horizon heuristic — misfires."""
    oauth_id = _seed("prod-1", priority=0, refresh_token="R0")
    _set_leased_until(oauth_id, -60, "job-1-attempt-1")

    token = _claim(lease_owner_for_job(2, 1), should_lease=lambda _t: False)

    assert token.id == oauth_id
    assert token.lease_owner is None and token.leased_until is None
    row = _row(oauth_id)
    assert row["lease_owner"] is None and row["leased_until"] is None


def test_credential_refresh_refuses_while_a_lease_is_unexpired():
    """A lease stops RESELECTION, not credential WRITES — so `register_credential` (which
    `credential:refresh` goes through) must refuse, or the leased job's own capture would
    later overwrite the operator's brand-new credential with a descendant of the old one."""
    oauth_id = _seed("prod-1", priority=0, refresh_token="R0")
    _set_leased_until(oauth_id, _LEASE_TTL, "job-7-attempt-1")
    before = _row(oauth_id)

    conn = _test_connection()
    try:
        with pytest.raises(CredentialLeasedError):
            register_credential(
                conn,
                scope="claude",
                label="prod-1",
                credentials={"subscription_key": "sk-operator", "refresh_token": "R-op"},
            )
        conn.rollback()
    finally:
        conn.close()

    after = _row(oauth_id)
    assert after["credentials"] == before["credentials"]
    assert after["lease_owner"] == "job-7-attempt-1"
    assert after["leased_until"] == before["leased_until"]

    # Once released, the same call succeeds.
    conn = _test_connection()
    try:
        assert release_credential_lease(conn, oauth_id, "job-7-attempt-1") is True
        conn.commit()
        register_credential(
            conn,
            scope="claude",
            label="prod-1",
            credentials={"subscription_key": "sk-operator", "refresh_token": "R-op"},
        )
        conn.commit()
        assert get_credential(conn, oauth_id).credentials["refresh_token"] == "R-op"
    finally:
        conn.close()


def test_register_credential_lease_expiry_is_evaluated_in_sql():
    """Boundary around `leased_until > UTC_TIMESTAMP()`, decided DB-side so a skewed host
    clock can neither refuse a long-dead lease nor write straight through a live one."""
    oauth_id = _seed("prod-1", priority=0, refresh_token="R0")

    _set_leased_until(oauth_id, -1, "job-7-attempt-1")
    conn = _test_connection()
    try:
        register_credential(
            conn,
            scope="claude",
            label="prod-1",
            credentials={"subscription_key": "sk-a", "refresh_token": "R-a"},
        )
        conn.commit()
    finally:
        conn.close()
    # An expired holder is also cleared, so diagnostics stop naming it.
    assert _row(oauth_id)["lease_owner"] is None

    _set_leased_until(oauth_id, 60, "job-8-attempt-1")
    conn = _test_connection()
    try:
        with pytest.raises(CredentialLeasedError):
            register_credential(
                conn,
                scope="claude",
                label="prod-1",
                credentials={"subscription_key": "sk-b", "refresh_token": "R-b"},
            )
        conn.rollback()
    finally:
        conn.close()


def test_ten_concurrent_auth_failures_do_not_escalate_beyond_one_outcome(
    int_db_config, int_consumer_config,
):
    """The reclassified 401, reported by all ten workers at once. Real MySQL, because the
    claim is about what a *wave* of concurrent reports converges on: with the phrase moved
    off the poison list the row ends up throttled and still ``ok``, never quarantined —
    which is exactly what a strike-counting design could not deliver (worker 2 would read
    worker 1's strike and escalate on the very first incident).
    """
    import logging

    credential_id = _seed("prod-1", priority=0, refresh_token="R0")
    credential = _claim(None)
    assert credential is not None and credential.id == credential_id

    consumer = Consumer(int_db_config, int_consumer_config, logging.getLogger("test"))
    barrier = threading.Barrier(10)

    def worker(n: int) -> None:
        barrier.wait()
        consumer._handle_transient_auth(
            SimpleNamespace(id=n, reference_id=f"AI-{n}"),
            credential,
            "claude",
            TransientAuthError("401 Invalid authentication credentials"),
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    row = _row(credential_id)
    assert row["status"] == "ok", "a replayed single-use secret must never poison the row"
    assert row["error_source"] is None
    assert row["throttled_until"] is not None
    assert row["throttled_until"] > datetime.now(UTC).replace(tzinfo=None)
