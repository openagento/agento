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
