"""Integration: mixed token methods rotate under concurrent selection."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Barrier

import pytest

from agento.framework.agent_manager.models import AgentProvider
from agento.framework.agent_manager.token_resolver import TokenResolver
from agento.framework.agent_manager.token_store import register_token
from agento.framework.db import get_connection

from .conftest import CONCURRENT_WORKERS_STRESS_TEST


def _seed_tokens(int_db_config, provider: AgentProvider, token_specs: list[tuple[str, str, dict]]) -> None:
    conn = get_connection(int_db_config)
    try:
        for label, token_type, credentials in token_specs:
            register_token(
                conn,
                provider,
                label,
                credentials,
                type=token_type,
            )
        conn.commit()
    finally:
        conn.close()


def _claim_token(int_db_config, provider: AgentProvider, barrier: Barrier) -> tuple[int, str, str]:
    conn = get_connection(int_db_config)
    try:
        barrier.wait(timeout=5)
        token = TokenResolver().resolve(conn, provider)
        return token.id, token.type, token.label
    finally:
        conn.close()


# Peak concurrent claimants exercised by the high-contention test. Every worker
# holds its own MySQL connection for the whole claim (there is no pooling — see
# framework/db.get_connection), so the server must allow this many plus headroom
# for fixtures and any other client.
_HIGH_CONTENTION_WORKERS = 200
_CONNECTION_HEADROOM = 30


def _server_max_connections(int_db_config) -> int:
    conn = get_connection(int_db_config)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW VARIABLES LIKE 'max_connections'")
            return int(cur.fetchone()["Value"])
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("provider", "token_specs"),
    [
        (
            AgentProvider.CLAUDE,
            [
                ("claude-oauth-a", "oauth", {
                    "subscription_key": "sk-claude-oauth-a",
                    "refresh_token": "rt-a",
                }),
                ("claude-oauth-b", "oauth", {
                    "subscription_key": "sk-claude-oauth-b",
                    "refresh_token": "rt-b",
                }),
                ("claude-api-key", "anthropic_api_key", {
                    "api_key": "sk-ant-api",
                }),
            ],
        ),
        (
            AgentProvider.CODEX,
            [
                ("codex-oauth", "oauth", {
                    "subscription_key": "codex-access",
                    "refresh_token": "codex-refresh",
                    "raw_auth": {"tokens": {"access_token": "codex-access"}},
                }),
                ("codex-access-token", "codex_access_token", {
                    "access_token": "eyJ.codex.access",
                }),
                ("codex-api-key", "openai_api_key", {
                    "api_key": "sk-openai-api",
                }),
            ],
        ),
    ],
)
def test_concurrent_selection_rotates_across_mixed_token_methods(
    int_db_config,
    provider: AgentProvider,
    token_specs: list[tuple[str, str, dict]],
):
    """Ten concurrent claims over three same-priority mixed-method tokens all
    succeed and keep the pool fair enough to use every healthy token.

    This exercises the real MySQL blocking ``FOR UPDATE`` claim path, including
    transient contention where a claimant briefly blocks on a locked token row.
    Distinctness under a *full* pool is covered by
    tests/integration/test_token_pool_concurrency.py.
    """
    _seed_tokens(int_db_config, provider, token_specs)

    barrier = Barrier(CONCURRENT_WORKERS_STRESS_TEST)
    with ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS_STRESS_TEST) as executor:
        futures = [
            executor.submit(_claim_token, int_db_config, provider, barrier)
            for _ in range(CONCURRENT_WORKERS_STRESS_TEST)
        ]
        claims = [future.result(timeout=10) for future in as_completed(futures)]

    labels = [label for _id, _type, label in claims]
    types = [token_type for _id, token_type, _label in claims]
    counts = Counter(labels)

    expected_labels = {label for label, _type, _credentials in token_specs}
    expected_types = {token_type for _label, token_type, _credentials in token_specs}

    assert set(labels) == expected_labels
    assert set(types) == expected_types
    assert sum(counts.values()) == CONCURRENT_WORKERS_STRESS_TEST
    assert len(counts) == 3
    assert min(counts.values()) >= 2

    conn = get_connection(int_db_config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT label, type, used_at FROM oauth_token "
                "WHERE agent_type = %s ORDER BY id",
                (provider.value,),
            )
            rows = cur.fetchall()
            cur.execute("SHOW COLUMNS FROM oauth_token LIKE 'used_at'")
            used_at_column = cur.fetchone()
    finally:
        conn.close()

    assert {(row["label"], row["type"]) for row in rows} == {
        (label, token_type) for label, token_type, _credentials in token_specs
    }
    assert all(row["used_at"] is not None for row in rows)
    assert used_at_column["Type"].lower() == "datetime(6)"


def test_high_contention_every_claimant_gets_a_token(int_db_config):
    """200 simultaneous claimants over 3 tokens: all succeed, none is refused.

    Saturation is the point — with far more workers than tokens, every healthy
    row is locked in passing for most of the run, so this exercises the
    contention path (``FOR UPDATE SKIP LOCKED`` returning nothing, then backing
    off) rather than the happy path. A claimant must queue, never be told the
    pool is exhausted while it is in fact healthy.

    Sized to the connection ceiling, not to a guess: each worker holds its own
    connection for the whole claim, and there is no pooling.
    """
    required = _HIGH_CONTENTION_WORKERS + _CONNECTION_HEADROOM
    available = _server_max_connections(int_db_config)
    if available < required:
        pytest.skip(
            f"MySQL max_connections={available} < {required} required for "
            f"{_HIGH_CONTENTION_WORKERS} concurrent claimants "
            f"(no connection pooling). Start the server with "
            f"--max-connections={required} to run this test."
        )

    _seed_tokens(int_db_config, AgentProvider.CLAUDE, [
        ("pool-a", "anthropic_api_key", {"api_key": "sk-a"}),
        ("pool-b", "anthropic_api_key", {"api_key": "sk-b"}),
        ("pool-c", "anthropic_api_key", {"api_key": "sk-c"}),
    ])

    barrier = Barrier(_HIGH_CONTENTION_WORKERS, timeout=120)
    with ThreadPoolExecutor(max_workers=_HIGH_CONTENTION_WORKERS) as executor:
        futures = [
            executor.submit(_claim_token, int_db_config, AgentProvider.CLAUDE, barrier)
            for _ in range(_HIGH_CONTENTION_WORKERS)
        ]
        claims = [future.result(timeout=300) for future in as_completed(futures)]

    counts = Counter(label for _id, _type, label in claims)

    # Every claimant got a token — the pool was healthy throughout, so nobody
    # may be refused with "all healthy tokens are currently locked".
    assert len(claims) == _HIGH_CONTENTION_WORKERS
    # Saturation still fans out across the whole pool rather than hot-spotting
    # one row; LRU ordering makes every token carry a real share of the load.
    assert set(counts) == {"pool-a", "pool-b", "pool-c"}
    assert min(counts.values()) >= _HIGH_CONTENTION_WORKERS // 10
