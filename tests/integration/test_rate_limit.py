"""The request limiter, against real MySQL (PRD E3-E5 §7.5, SEC-12).

Real MySQL and not a fake, because the whole design of the limiter is one statement doing
the reset-and-increment: a fake would test the test.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from agento.web import rate_limit

from .conftest import _test_connection

CFG = {
    "window_seconds": 60,
    "max_requests_per_user": 5,
    "max_requests_per_address": 3,
    "auth_failures_before_hold": 2,
    "hold_seconds": 900,
    "bucket_retention_seconds": 300,
}


@pytest.fixture
def conn():
    c = _test_connection(autocommit=True)
    with c.cursor() as cur:
        cur.execute("DELETE FROM limit_bucket")
    yield c
    with c.cursor() as cur:
        cur.execute("DELETE FROM limit_bucket")
    c.close()


def _address(value: str) -> list[rate_limit.Bucket]:
    return rate_limit.request_buckets(address=value, session_token=None, launch_token=None)


def test_the_configured_limits_resolve(conn):
    got = rate_limit.limits(conn)
    assert got["window_seconds"] == 60
    assert got["max_requests_per_user"] == 120
    assert set(got) == set(rate_limit.LIMIT_KEYS)


@pytest.mark.parametrize("path", rate_limit.EXEMPT_PATHS)
def test_health_is_the_only_named_exemption(path):
    assert path == "/health"


def test_the_address_budget_runs_out_as_a_SHARED_refusal(conn):
    """An address is shared - a NAT, an office, a proxy. Its ceiling therefore comes back as
    a shared refusal the listener applies only to a caller that proved no identity, never as
    a flat refusal: SEC-12 says one caller behind an address cannot throttle the others'
    authorized traffic."""
    buckets = _address("198.51.100.7")
    for _ in range(CFG["max_requests_per_address"]):
        assert rate_limit.check(conn, buckets, cfg=CFG).shared_refusal == 0
    over = rate_limit.check(conn, buckets, cfg=CFG)
    assert over.allowed and over.shared_refusal > 0


def test_an_invalid_session_does_not_spend_a_valid_users_budget(conn):
    """A stranger's failures land on their own session and address buckets."""
    stranger = rate_limit.request_buckets(
        address="198.51.100.8", session_token="not-a-session", launch_token=None)
    for _ in range(CFG["max_requests_per_address"]):
        rate_limit.check(conn, stranger, cfg=CFG)
        rate_limit.record_auth_failure(conn, stranger, cfg=CFG)

    assert rate_limit.count_identity(conn, 42, cfg=CFG).allowed


def test_rotating_the_credential_does_not_evade_the_user_bucket(conn):
    for _ in range(CFG["max_requests_per_user"]):
        fresh = rate_limit.request_buckets(
            address="198.51.100.9", session_token=f"token-{_}", launch_token=None)
        rate_limit.check(conn, fresh, cfg=CFG)
        assert rate_limit.count_identity(conn, 7, cfg=CFG).allowed
    assert not rate_limit.count_identity(conn, 7, cfg=CFG).allowed


def test_rotating_the_address_does_not_evade_the_session_bucket(conn):
    for n in range(CFG["max_requests_per_user"]):
        buckets = rate_limit.request_buckets(
            address=f"203.0.113.{n}", session_token="one-session", launch_token=None)
        assert rate_limit.check(conn, buckets, cfg=CFG).allowed
    last = rate_limit.request_buckets(
        address="203.0.113.200", session_token="one-session", launch_token=None)
    assert not rate_limit.check(conn, last, cfg=CFG).allowed


def test_concurrent_requests_at_one_bucket_lose_no_increment(conn):
    buckets = _address("198.51.100.10")
    workers = 25

    def hit():
        own = _test_connection(autocommit=True)
        try:
            rate_limit.check(own, buckets, cfg={**CFG, "max_requests_per_address": 10**6})
        finally:
            own.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda _: hit(), range(workers)))

    with conn.cursor() as cur:
        cur.execute("SELECT request_count FROM limit_bucket WHERE bucket_kind = 'address'")
        assert cur.fetchone()["request_count"] == workers


def test_a_shared_hold_is_reported_and_not_refused_here(conn):
    """The limiter runs before the route, so it cannot know who is calling.

    A hold on a bucket many callers share therefore comes back as `shared_refusal` and NOT
    as a refusal: the listener applies it to a request that proved no identity, and a
    caller that did authenticate is not held for a stranger behind the same address.
    """
    buckets = _address("198.51.100.11")
    rate_limit.check(conn, buckets, cfg=CFG)
    for _ in range(CFG["auth_failures_before_hold"]):
        rate_limit.record_auth_failure(conn, buckets, cfg=CFG)

    decision = rate_limit.check(conn, buckets,
                                cfg={**CFG, "max_requests_per_address": 10**6})
    assert decision.allowed and decision.shared_refusal > 0


def test_a_private_hold_stops_its_own_caller_even_authenticated(conn):
    buckets = rate_limit.request_buckets(
        address="198.51.100.12", session_token="held-session", launch_token=None)
    rate_limit.check(conn, buckets, cfg=CFG)
    session_only = [b for b in buckets if b.kind == "session"]
    for _ in range(CFG["auth_failures_before_hold"]):
        rate_limit.record_auth_failure(conn, session_only, cfg=CFG)

    refused = rate_limit.check(conn, session_only, cfg=CFG)
    assert not refused.allowed and refused.shared_refusal == 0


def test_expires_at_covers_both_the_window_and_the_hold(conn):
    """The SEC-9 cross-field invariant, re-checked after every write, not only at boot."""
    cfg = {**CFG, "window_seconds": 3600, "bucket_retention_seconds": 300}
    buckets = _address("198.51.100.13")
    rate_limit.check(conn, buckets, cfg=cfg)
    for _ in range(cfg["auth_failures_before_hold"]):
        rate_limit.record_auth_failure(conn, buckets, cfg=cfg)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT expires_at >= GREATEST(window_start + INTERVAL %s SECOND, held_until) "
            "       + INTERVAL %s SECOND AS ok, "
            "       request_count, held_until IS NOT NULL AS held "
            "FROM limit_bucket WHERE bucket_kind = 'address'",
            (cfg["window_seconds"], cfg["bucket_retention_seconds"]),
        )
        row = cur.fetchone()
    assert row["ok"] == 1
    # The longest window with the shortest retention keeps BOTH the count and the hold.
    assert row["request_count"] == 1 and row["held"] == 1


def test_a_stale_window_resets_the_count_and_the_failures(conn):
    buckets = _address("198.51.100.14")
    rate_limit.check(conn, buckets, cfg=CFG)
    rate_limit.record_auth_failure(conn, buckets, cfg=CFG)
    with conn.cursor() as cur:
        cur.execute("UPDATE limit_bucket SET window_start = NOW() - INTERVAL 1 HOUR")

    assert rate_limit.check(conn, buckets, cfg=CFG).allowed
    with conn.cursor() as cur:
        cur.execute("SELECT request_count, auth_failures FROM limit_bucket")
        assert cur.fetchone() == {"request_count": 1, "auth_failures": 0}


def test_expired_buckets_are_pruned(conn):
    for n in range(20):
        rate_limit.check(conn, _address(f"192.0.2.{n}"), cfg=CFG)
    with conn.cursor() as cur:
        cur.execute("UPDATE limit_bucket SET expires_at = NOW() - INTERVAL 1 SECOND")

    assert rate_limit.prune(conn) == 20
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM limit_bucket")
        assert cur.fetchone()["n"] == 0


def test_a_live_bucket_survives_the_prune(conn):
    rate_limit.check(conn, _address("192.0.2.200"), cfg=CFG)
    assert rate_limit.prune(conn) == 0


def test_a_bucket_key_is_never_the_credential(conn):
    buckets = rate_limit.request_buckets(
        address="198.51.100.15", session_token="s3cret-token", launch_token="l4unch-token")
    rate_limit.check(conn, buckets, cfg=CFG)
    with conn.cursor() as cur:
        cur.execute("SELECT bucket_key FROM limit_bucket")
        keys = [r["bucket_key"] for r in cur.fetchall()]
    assert all(len(k) == 64 for k in keys)
    assert "s3cret-token" not in keys and "l4unch-token" not in keys


# --- the real core/limits/* fields, through every source ----------------------------

_SAFE_DIRECTION = {
    "window_seconds": 60,               # 1 would mean 120 requests per SECOND
    "max_requests_per_user": 120,
    "max_requests_per_address": 60,
    "auth_failures_before_hold": 10,
    "hold_seconds": 900,                # 1 would make a hold meaningless
    "bucket_retention_seconds": 3600,
}


@pytest.mark.parametrize("key", rate_limit.LIMIT_KEYS)
@pytest.mark.parametrize("bad", ["0", "abc", "-1", "99999999"])
def test_no_env_value_can_weaken_a_limit(conn, monkeypatch, key, bad):
    monkeypatch.setenv(f"CONFIG__CORE__LIMITS__{key.upper()}", bad)
    assert rate_limit.limits(conn)[key] == _SAFE_DIRECTION[key]


@pytest.mark.parametrize("key", rate_limit.LIMIT_KEYS)
@pytest.mark.parametrize("bad", ["0", "abc"])
def test_no_legacy_db_row_can_weaken_a_limit(conn, key, bad):
    """A row written before the bound existed must not be served."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core_config_data (scope, scope_id, path, value, encrypted) "
            "VALUES ('default', 0, %s, %s, 0) ON DUPLICATE KEY UPDATE value = VALUES(value)",
            (f"core/limits/{key}", bad),
        )
    try:
        assert rate_limit.limits(conn)[key] == _SAFE_DIRECTION[key]
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM core_config_data WHERE path = %s", (f"core/limits/{key}",))


# No single number is inside all six ranges (retention starts at 300, the failure count
# stops at 100), so each key gets its own in-range value.
_IN_RANGE = {
    "window_seconds": 30,
    "max_requests_per_user": 500,
    "max_requests_per_address": 500,
    "auth_failures_before_hold": 20,
    "hold_seconds": 60,
    "bucket_retention_seconds": 600,
}


@pytest.mark.parametrize("key", rate_limit.LIMIT_KEYS)
def test_a_value_inside_the_bounds_is_honoured(conn, key):
    """The substitution narrows nothing that was valid."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core_config_data (scope, scope_id, path, value, encrypted) "
            "VALUES ('default', 0, %s, %s, 0) ON DUPLICATE KEY UPDATE value = VALUES(value)",
            (f"core/limits/{key}", str(_IN_RANGE[key])),
        )
    try:
        assert rate_limit.limits(conn)[key] == _IN_RANGE[key]
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM core_config_data WHERE path = %s", (f"core/limits/{key}",))


# --- persistence (SEC-12) ---------------------------------------------------
#
# Every test above uses an autocommit connection, which is what hid this: production
# connections are `autocommit=False` (`framework/db.py`) and the listener closes the
# request connection without committing, so a counter written and not committed is a
# counter rolled back. The guard is the SHAPE, not a `.commit()` call - each write is
# read back over a SECOND connection, which can only see committed rows.

def _committed_count(bucket_kind: str, bucket_key: str) -> dict | None:
    other = _test_connection(autocommit=True)
    try:
        with other.cursor() as cur:
            cur.execute("SELECT request_count, auth_failures, held_until FROM limit_bucket "
                        "WHERE bucket_kind = %s AND bucket_key = %s", (bucket_kind, bucket_key))
            return cur.fetchone()
    finally:
        other.close()


@pytest.fixture
def txn_conn(conn):
    """A production-shaped connection: nothing it writes is visible until it commits."""
    c = _test_connection(autocommit=False)
    yield c
    if c.open:
        c.close()


def test_counting_survives_a_connection_that_never_commits(txn_conn):
    buckets = _address("198.51.100.21")
    rate_limit.check(txn_conn, buckets, cfg=CFG)
    rate_limit.check(txn_conn, buckets, cfg=CFG)
    txn_conn.close()                       # the listener closes; it never commits

    assert _committed_count("address", buckets[0].key)["request_count"] == 2


def test_a_failed_authentication_survives_a_connection_that_never_commits(txn_conn):
    buckets = _address("198.51.100.22")
    rate_limit.check(txn_conn, buckets, cfg=CFG)
    for _ in range(CFG["auth_failures_before_hold"]):
        rate_limit.record_auth_failure(txn_conn, buckets, cfg=CFG)
    txn_conn.close()

    row = _committed_count("address", buckets[0].key)
    assert row["auth_failures"] == CFG["auth_failures_before_hold"]
    assert row["held_until"] is not None   # the hold outlives the request that earned it


def test_pruning_survives_a_connection_that_never_commits(txn_conn):
    buckets = _address("198.51.100.23")
    rate_limit.check(txn_conn, buckets, cfg={**CFG, "window_seconds": 1,
                                             "bucket_retention_seconds": 0})
    txn_conn.commit()
    with txn_conn.cursor() as cur:
        cur.execute("UPDATE limit_bucket SET expires_at = NOW() - INTERVAL 1 SECOND "
                    "WHERE bucket_key = %s", (buckets[0].key,))
    txn_conn.commit()

    assert rate_limit.prune(txn_conn, cfg=CFG) == 1
    txn_conn.close()

    assert _committed_count("address", buckets[0].key) is None


def test_a_failure_on_a_bucket_this_request_never_counted_still_lands(conn):
    """SEC-12. An UPDATE silently matches nothing when the row does not exist yet, and the
    row does not exist on a route that counts only its private buckets - which is every route
    the proxy alone can reach. The failure that places the hold cannot be the one write that
    is allowed to miss, so the write is an upsert."""
    buckets = _address("10.0.0.9")
    for _ in range(CFG["auth_failures_before_hold"]):
        rate_limit.record_auth_failure(conn, buckets, cfg=CFG)

    assert rate_limit.held(conn, buckets) > 0
    with conn.cursor() as cur:
        cur.execute("SELECT request_count, auth_failures FROM limit_bucket "
                    "WHERE bucket_kind = %s AND bucket_key = %s", (buckets[0].kind, buckets[0].key))
        row = cur.fetchone()
    # The row was born of a failure: it counts the failures, and no request.
    assert (row["request_count"], row["auth_failures"]) == (0, CFG["auth_failures_before_hold"])


def test_reading_a_hold_counts_nothing(conn):
    """`held` is for a caller that must not spend the shared ceiling - so it must not."""
    buckets = _address("10.0.0.10")
    for _ in range(CFG["max_requests_per_address"] + 3):
        assert rate_limit.held(conn, buckets) == 0
    assert rate_limit.check(conn, buckets, cfg=CFG).allowed       # budget untouched


def test_a_hold_on_an_unknown_bucket_is_zero(conn):
    assert rate_limit.held(conn, _address("10.0.0.11")) == 0
    assert rate_limit.held(conn, []) == 0


def test_a_failure_only_bucket_resets_its_window(conn):
    """SEC-12. The window is a window on this write too. A bucket that only ever sees
    failures - the proxy's authz route counts no request on the shared bucket - is never
    passed through `_touch`, so if this write did not reset a stale window, failures from
    windows days apart would add up into one hold that nothing earned."""
    buckets = _address("10.0.0.12")
    for _ in range(CFG["auth_failures_before_hold"] - 1):
        rate_limit.record_auth_failure(conn, buckets, cfg=CFG)
    with conn.cursor() as cur:                       # the window has passed
        cur.execute("UPDATE limit_bucket SET window_start = NOW() - INTERVAL %s SECOND",
                    (CFG["window_seconds"] + 5,))

    rate_limit.record_auth_failure(conn, buckets, cfg=CFG)

    assert rate_limit.held(conn, buckets) == 0       # the first failure of a NEW window
    with conn.cursor() as cur:
        cur.execute("SELECT auth_failures, held_until, "
                    "  TIMESTAMPDIFF(SECOND, NOW(), expires_at) AS ttl, "
                    "  TIMESTAMPDIFF(SECOND, NOW(), window_start) AS age FROM limit_bucket")
        row = cur.fetchone()
    assert row["auth_failures"] == 1
    assert row["held_until"] is None
    assert row["age"] == 0                                        # the window moved
    assert row["ttl"] > CFG["bucket_retention_seconds"]           # expiry refreshed with it

    # And the hold still arrives, once the threshold is met INSIDE one window.
    for _ in range(CFG["auth_failures_before_hold"] - 1):
        rate_limit.record_auth_failure(conn, buckets, cfg=CFG)
    assert rate_limit.held(conn, buckets) > 0
