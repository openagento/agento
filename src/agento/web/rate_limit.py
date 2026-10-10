"""The request limiter every route passes through (PRD E3-E5 §7.5, SEC-12).

DB-backed on purpose: an in-process counter limits one replica each, so the panel's real
limit would be the configured number times the number of replicas. The counters live in
`limit_bucket` and every increment is ONE statement, so two concurrent connections at one
bucket cannot lose an increment.

Three rules this module exists to keep:

* A bucket key is always a hash, never the credential it was derived from.
* A request is counted on every bucket it is GIVEN, so rotating the credential does not
  evade the address bucket. What it is given is the listener's decision: the PRIVATE buckets
  before the identity is known, the SHARED one only for a caller that proved none.
* The SHARED bucket (address / fallback) is therefore never spent by authorized traffic, and
  a refusal from it - its hold OR its ceiling - stops unauthenticated and failed
  authentication only. Many callers sit behind one address (today, behind one proxy), and
  SEC-12 is explicit: the address limit counts failures only, so one caller cannot throttle
  the others' authorized traffic - or lock a stranger out of signing in. The listener counts
  and applies it before dispatch, once it knows who is calling. A caller that must not spend that
  ceiling but must still obey its hold - the proxy's `forward_auth` subrequest, one per forwarded
  request - reads it with `held()` instead.
* A failed authentication is recorded on a bucket whether or not this request counted it, so the
  write is an upsert: on a route that only ever counts its private buckets, the failure is the
  shared bucket's first sight of the caller.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from agento.framework.config_resolver import load_db_overrides, read_config_defaults, resolve_field

# The single exemption, named here rather than left to each route to claim: the proxy and
# Compose poll it, and a limited /health turns a burst into an unhealthy container.
EXEMPT_PATHS = ("/health",)

LIMIT_KEYS = (
    "window_seconds",
    "max_requests_per_user",
    "max_requests_per_address",
    "auth_failures_before_hold",
    "hold_seconds",
    "bucket_retention_seconds",
)

# Kinds that belong to ONE caller. Everything else is shared, and a shared refusal never
# reaches a request that proved an identity.
PRIVATE_KINDS = ("user", "session", "launch")

# One expression, used by every write, so the cross-field invariant of §7.5 cannot drift
# between them: expires_at = max(window_end, held_until) + bucket_retention_seconds.
_EXPIRES_AT = (
    "GREATEST(window_start + INTERVAL %s SECOND, IFNULL(held_until, window_start)) "
    "+ INTERVAL %s SECOND"
)

# The window is a WINDOW: a counter from a window that has passed is not evidence about
# this one. Every write that increments a counter resets the row first when this is true,
# and reads the OLD `window_start` to decide it - so it must be evaluated before the
# assignment that moves it (MySQL applies assignments left to right).
_STALE = "window_start <= NOW() - INTERVAL %s SECOND"


@dataclass(frozen=True)
class Bucket:
    kind: str
    key: str          # already hashed: this dataclass never holds a credential

    @property
    def private(self) -> bool:
        return self.kind in PRIVATE_KINDS


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int = 0
    # Seconds to wait when a SHARED bucket refuses - its hold OR its ceiling. It is not
    # a refusal here: many callers sit behind one address, so refusing on it before
    # knowing who is calling would let one stranger throttle everyone else's authorized
    # traffic (SEC-12: "the address limit counts failures only"). The listener resolves
    # the identity and then applies this, before any handler runs.
    shared_refusal: int = 0


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def bucket(kind: str, value: str) -> Bucket:
    return Bucket(kind=kind, key=digest(value))


def limits(conn) -> dict[str, int]:
    """The six `core/limits/*` bounds, resolved ENV -> DB -> config.json.

    The resolver substitutes the validated `config.json` default for an out-of-bounds or
    unparseable ENV/DB value, so no source can widen or switch off the limiter.
    """
    from agento.framework.bootstrap import CORE_MODULES_DIR

    core = Path(CORE_MODULES_DIR) / "core"
    schema = json.loads((core / "system.json").read_text())
    defaults = read_config_defaults(core)
    overrides = load_db_overrides(conn)
    return {
        key: int(resolve_field("core", f"limits/{key}", schema[f"limits/{key}"],
                               defaults, overrides).value)
        for key in LIMIT_KEYS
    }


def request_buckets(*, address: str | None, session_token: str | None,
                    launch_token: str | None) -> list[Bucket]:
    """The buckets known BEFORE the request is authenticated.

    The credentials are hashed, never stored or compared here; the user bucket is added
    later by `count_identity`, once the session actually resolves.
    """
    buckets = [bucket("address", address or "unknown")]
    if session_token:
        buckets.append(bucket("session", session_token))
    if launch_token:
        buckets.append(bucket("launch", launch_token))
    return buckets


def _touch(conn, bucket_: Bucket, cfg: dict[str, int]) -> tuple[int, bool, int]:
    """Count one request against a bucket. Returns (count, held, retry_after).

    The INSERT ... ON DUPLICATE KEY UPDATE is the whole increment: it resets a stale window
    and increments in one round trip. Assignments run left to right, so `request_count` and
    `auth_failures` read the OLD `window_start` and `expires_at` reads the new one.
    """
    window = cfg["window_seconds"]
    retention = cfg["bucket_retention_seconds"]
    stale = _STALE
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO limit_bucket (bucket_key, bucket_kind, window_start, request_count, "
            "auth_failures, expires_at) VALUES (%s, %s, NOW(), 1, 0, "
            "NOW() + INTERVAL %s SECOND + INTERVAL %s SECOND) "
            "ON DUPLICATE KEY UPDATE "
            f"  request_count = LAST_INSERT_ID(IF({stale}, 1, request_count + 1)), "
            f"  auth_failures = IF({stale}, 0, auth_failures), "
            f"  window_start = IF({stale}, NOW(), window_start), "
            f"  expires_at = {_EXPIRES_AT}",
            (bucket_.key, bucket_.kind, window, retention,
             window, window, window, window, retention),
        )
        # rowcount is 1 when the row was inserted, 2 when it was updated (MySQL's
        # documented ON DUPLICATE KEY UPDATE contract). On an insert the count is 1 and
        # LAST_INSERT_ID() holds the new id, not the counter.
        # This reads the AFFECTED-rows count, so no connection here may set pymysql's
        # CLIENT.FOUND_ROWS: with it an update also reports 1, every request would read as
        # count 1, and the limiter would never deny. No connection in the repo sets it.
        inserted = cur.rowcount == 1
        cur.execute("SELECT LAST_INSERT_ID() AS n")
        row = cur.fetchone()
        count = 1 if inserted else int(row["n"] if isinstance(row, dict) else row[0])
        cur.execute(
            "SELECT GREATEST(TIMESTAMPDIFF(SECOND, NOW(), held_until), 0) AS hold, "
            "GREATEST(TIMESTAMPDIFF(SECOND, NOW(), window_start + INTERVAL %s SECOND), 0) AS win "
            "FROM limit_bucket WHERE bucket_kind = %s AND bucket_key = %s",
            (window, bucket_.kind, bucket_.key),
        )
        row = cur.fetchone() or {}
        hold = int((row.get("hold") if isinstance(row, dict) else row[0]) or 0)
        window_left = int((row.get("win") if isinstance(row, dict) else row[1]) or 0)
    return count, hold > 0, max(hold, window_left) or 1


def check(conn, buckets: list[Bucket], *,
          cfg: dict[str, int] | None = None) -> Decision:
    """Count the request on every bucket and answer once, refusing on the first violation.

    Every bucket is counted even when an earlier one already refused: a refusal that
    stopped counting would let a caller spend another bucket's budget for free.

    A violation is ANSWERED here only on a PRIVATE bucket - session, launch, user - because
    only those belong to one caller. A shared bucket's hold and its ceiling both come back
    as `shared_refusal` instead, for two different reasons that have the same answer:

    * many callers sit behind one address (today, behind one proxy), and SEC-12 is explicit
      that the address limit must not throttle their authorized traffic;
    * this runs before the route, so it cannot know whether the request proves an identity,
      and PRESENTING a credential is not proving one - a bogus cookie is exactly what a
      brute-force attempt carries.

    So the listener resolves the identity itself and applies `shared_refusal` to what did
    not authenticate, BEFORE the handler runs (`server._refused_as_a_stranger`).
    """
    cfg = cfg or limits(conn)
    refusal: Decision | None = None
    shared_refusal = 0
    for b in buckets:
        count, held, retry_after = _touch(conn, b, cfg)
        ceiling = (cfg["max_requests_per_user"] if b.private
                   else cfg["max_requests_per_address"])
        violated = held or count > ceiling
        if not violated:
            continue
        if b.private:
            refusal = refusal or Decision(False, retry_after)
        else:
            shared_refusal = max(shared_refusal, retry_after)
    # Counting is a write, and this connection is not autocommit (`framework/db.py`): the
    # request handler closes it without committing, which would roll every increment back
    # and leave the limiter counting nothing. Committing here also keeps the count when
    # the handler below fails - a refused request is still a request.
    conn.commit()
    if refusal is not None:
        return refusal
    return Decision(True, shared_refusal, shared_refusal)


def count_identity(conn, user_id: int, *, cfg: dict[str, int] | None = None) -> Decision:
    """Count the request on the resolved user's bucket, once the session has resolved.

    This is what makes rotating the session token useless: a new token is a new session
    bucket, but the user bucket is the same one.
    """
    return check(conn, [bucket("user", str(user_id))], cfg=cfg)


def held(conn, buckets: list[Bucket]) -> int:
    """Seconds left on the longest hold across these buckets - reading only, counting nothing.

    For a caller whose request must NOT spend the shared ceiling but must still be stopped by
    a shared hold. The proxy's `forward_auth` subrequest is that caller: one arrives for every
    request the proxy handles, so counting them would exhaust the shared budget on ordinary
    traffic, while ignoring the hold would make the hold unreachable on exactly the route being
    brute-forced (SEC-12).
    """
    if not buckets:
        return 0
    marks = ", ".join(["(%s, %s)"] * len(buckets))
    args: list[str] = []
    for b in buckets:
        args += [b.kind, b.key]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(GREATEST(TIMESTAMPDIFF(SECOND, NOW(), held_until), 0)) AS hold "
            f"FROM limit_bucket WHERE (bucket_kind, bucket_key) IN ({marks})", args)
        row = cur.fetchone() or {}
    conn.commit()
    return int((row.get("hold") if isinstance(row, dict) else row[0]) or 0)


def record_auth_failure(conn, buckets: list[Bucket], *,
                        cfg: dict[str, int] | None = None) -> None:
    """Count a failed authentication and place a hold once the threshold is reached.

    An UPSERT, not an UPDATE: a bucket this request never counted has no row yet, and an
    UPDATE would silently match nothing. That is not hypothetical - a route reached only by
    the proxy's subrequest counts its private buckets and never touches the shared one, so
    the shared bucket's first failure is also the row's first sight of it (SEC-12).
    """
    cfg = cfg or limits(conn)
    window, retention = cfg["window_seconds"], cfg["bucket_retention_seconds"]
    threshold, hold = cfg["auth_failures_before_hold"], cfg["hold_seconds"]
    # The first failure holds only when the threshold is 1; expressed once, used by the
    # INSERT for both `held_until` and the `expires_at` it feeds.
    first_hold = "IF(%s <= 1, NOW() + INTERVAL %s SECOND, NULL)"
    for b in buckets:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO limit_bucket (bucket_key, bucket_kind, window_start, "
                "  request_count, auth_failures, held_until, expires_at) "
                f"VALUES (%s, %s, NOW(), 0, 1, {first_hold}, "
                f"  GREATEST(NOW() + INTERVAL %s SECOND, IFNULL({first_hold}, NOW())) "
                "  + INTERVAL %s SECOND) "
                "ON DUPLICATE KEY UPDATE "
                # Assignments run left to right, so held_until must be computed BEFORE the
                # increment: after it, `auth_failures` is already the new value and the
                # hold would fire one failure early. Both it and the counter reset a stale
                # window, exactly as `_touch` does - a bucket written ONLY here (the proxy's
                # authz route never counts a request on it) would otherwise accumulate
                # failures from windows days apart into one hold.
                f"  held_until = IF({_STALE}, "
                "      IF(1 >= %s, NOW() + INTERVAL %s SECOND, held_until), "
                "      IF(auth_failures + 1 >= %s, NOW() + INTERVAL %s SECOND, held_until)), "
                f"  auth_failures = IF({_STALE}, 1, auth_failures + 1), "
                f"  window_start = IF({_STALE}, NOW(), window_start), "
                f"  expires_at = {_EXPIRES_AT}",
                (b.key, b.kind, threshold, hold,
                 window, threshold, hold, retention,
                 window, threshold, hold, threshold, hold,
                 window, window, window, retention),
            )
    conn.commit()


def prune(conn, *, cfg: dict[str, int] | None = None) -> int:
    """Drop expired buckets. A flood of distinct addresses must not grow the table forever."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM limit_bucket WHERE expires_at <= NOW()")
        count = cur.rowcount
    conn.commit()
    return count
