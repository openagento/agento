"""Integration: the LRU token pool must hand DISTINCT tokens to concurrent
claimants when the pool is large enough to satisfy them.

Regression for a race in ``select_token``: ``SELECT ... LIMIT 1 FOR UPDATE
SKIP LOCKED`` over a filesorted scan (the ``expires_at``/``throttled_until``
OR-predicates defeat the pool index, forcing ``type=ALL`` + filesort) does NOT
guarantee two concurrent transactions receive distinct rows — ~1/3 of tightly
synchronized 10-way bursts handed the same row to two workers, defeating the
LRU fairness the pool exists for exactly under the concurrency that matters.
The claim must be atomic so N workers over an N-token pool each get their own.
"""
from __future__ import annotations

import threading

from agento.framework.agent_manager.models import AgentProvider, encrypt_credentials
from agento.framework.agent_manager.token_resolver import TokenResolver
from agento.framework.db import get_connection

from .conftest import CONCURRENT_WORKERS_STRESS_TEST, _test_connection

N = CONCURRENT_WORKERS_STRESS_TEST
BURSTS = 25


def _reset_pool(n: int) -> None:
    """Truncate the pool and seed ``n`` enabled, healthy, never-used claude
    tokens (``used_at`` NULL) so every burst is a fresh LRU race."""
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute("TRUNCATE TABLE oauth_token")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            for i in range(n):
                creds = encrypt_credentials({"subscription_key": f"sk-oauth-{i}"})
                cur.execute(
                    """INSERT INTO oauth_token
                           (agent_type, type, label, credentials, enabled, status, priority)
                       VALUES ('claude', 'oauth', %s, %s, TRUE, 'ok', 0)""",
                    (f"tok-{i}", creds),
                )
    finally:
        conn.close()


def _run_claim_burst(int_db_config, resolver: TokenResolver) -> tuple[list[str | None], list[str]]:
    """Release N claimants at a barrier so they overlap tightly, each resolving
    one token. Returns (subscription_keys, errors). ``barrier``/``results``/
    ``errors`` are function-local so the ``worker`` closure never binds a loop
    variable (ruff B023)."""
    results: list[str | None] = [None] * N
    errors: list[str] = []
    barrier = threading.Barrier(N)

    def worker(idx: int) -> None:
        conn = get_connection(int_db_config)
        try:
            # release all N claimants at once — force tight overlap. Timeout so a
            # thread that dies before reaching the barrier can't hang the other
            # N-1 forever; a broken barrier surfaces as an error (asserted below).
            barrier.wait(timeout=5)
            token = resolver.resolve(conn, AgentProvider.CLAUDE)
            results[idx] = token.credentials["subscription_key"]
        except Exception as exc:  # surface any claim failure (asserted below)
            errors.append(repr(exc))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_concurrent_token_claims_over_full_pool_are_distinct(int_db_config):
    resolver = TokenResolver()

    for burst in range(BURSTS):
        _reset_pool(N)
        results, errors = _run_claim_burst(int_db_config, resolver)

        assert not errors, f"burst {burst}: token resolution errored: {errors}"
        distinct = len({r for r in results})
        assert distinct == N, (
            f"burst {burst}: only {distinct} distinct tokens for {N} concurrent "
            f"workers over an {N}-token pool — two workers raced onto the same "
            f"token: {sorted(str(r) for r in results)}"
        )
