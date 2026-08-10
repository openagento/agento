"""Integration: an AUTOMATIC credential quarantine self-clears on the next successful run; an
operator's never does.

Real MySQL, because the whole guarantee is the ``AND error_source = 'auto'`` predicate and
the ENUM's NULL default for pre-migration rows.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.agent_manager.credential_store import (
    clear_auto_credential_error,
    clear_credential_error,
    mark_credential_error,
)
from agento.framework.agent_manager.models import encrypt_credentials
from agento.framework.consumer import Consumer

from .conftest import _test_connection


def _seed_errored(label: str, *, error_source: str | None) -> int:
    """A quarantined row. ``error_source=None`` is exactly the state migration 034 leaves a
    pre-existing quarantine in — provenance unknown, therefore operator-owned."""
    creds = encrypt_credentials({"subscription_key": "sk", "refresh_token": "R0"})
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO credential
                    (scope, agent_type, type, label, credentials, enabled, status, error_msg,
                     error_source, priority)
                VALUES ('claude', 'claude', 'oauth', %s, %s, TRUE, 'error', '401', %s, 0)
                """,
                (label, creds, error_source),
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


@pytest.fixture(autouse=True)
def _clean_tokens():
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM credential")
    finally:
        conn.close()
    yield


def test_auto_quarantine_self_heals_on_the_next_successful_run(sample_db_config):
    """The self-heal trigger is a SUCCESSFUL RUN, not a rotation: a completed run proves
    the credential works, whereas a run can rotate and still 401. On the 2026-07-15
    timeline this is what would have lifted the 16:13 quarantine at 16:38, unattended."""
    token_id = _seed_errored("prod-1", error_source="auto")
    consumer = Consumer(sample_db_config, MagicMock(), logging.getLogger("test"))

    with patch(
        "agento.framework.consumer.get_connection",
        side_effect=lambda _cfg: _test_connection(),
    ):
        consumer._finish_credential_lifecycle(
            SimpleNamespace(id=42),
            "claude",
            SimpleNamespace(id=token_id),
            None,  # no home_dir -> no capture, only the self-heal
            success=True,
        )

    row = _row(token_id)
    assert row["status"] == "ok"
    assert row["error_msg"] is None
    assert row["error_source"] is None


def test_operator_quarantine_is_never_auto_cleared():
    """The DECISIONS.md guard: ``update_refreshed_credentials``/self-heal must not resurrect
    a credential an operator deliberately took out of rotation."""
    token_id = _seed_errored("prod-1", error_source="operator")

    conn = _test_connection()
    try:
        assert clear_auto_credential_error(conn, token_id) is False
        conn.commit()
    finally:
        conn.close()

    assert _row(token_id)["status"] == "error"


def test_pre_existing_errored_row_is_not_resurrected_by_the_migration():
    """Production id=1's post-034 state: ``status='error'`` with NULL provenance. Unknown is
    treated as operator, so it needs one explicit ``credential:reset`` — the migration itself
    resurrects nothing."""
    token_id = _seed_errored("prod-1", error_source=None)

    conn = _test_connection()
    try:
        assert clear_auto_credential_error(conn, token_id) is False
        conn.commit()
        # An operator's reset still works, and clears provenance for the future.
        assert clear_credential_error(conn, token_id) is True
        conn.commit()
    finally:
        conn.close()

    row = _row(token_id)
    assert row["status"] == "ok"
    assert row["error_source"] is None


def test_mark_credential_error_records_who_quarantined_the_credential():
    """Provenance is what makes the self-heal safe; the CLI/admin paths keep the default."""
    token_id = _seed_errored("prod-1", error_source=None)

    conn = _test_connection()
    try:
        mark_credential_error(conn, token_id, "framework saw a dead credential", source="auto")
        conn.commit()
        assert _row(token_id)["error_source"] == "auto"

        mark_credential_error(conn, token_id, "operator says this licence is gone")
        conn.commit()
    finally:
        conn.close()

    assert _row(token_id)["error_source"] == "operator"
