from __future__ import annotations

import logging

import pymysql

from .models import CredentiallessUsage, UsageSummary


def record_usage(
    conn: pymysql.Connection,
    credential_id: int | None,
    tokens_used: int,
    input_tokens: int,
    output_tokens: int,
    reference_id: str | None = None,
    duration_ms: int = 0,
    model: str | None = None,
    harness: str | None = None,
    provider: str | None = None,
    logger: logging.Logger | None = None,
) -> int:
    """Insert a usage record. Returns the inserted row ID.

    ``credential_id`` is ``None`` for a run made by a provider that requires no
    credential — the row is still recorded, attributed by ``(harness, provider)``.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO usage_log
                (credential_id, harness, provider, tokens_used, input_tokens,
                 output_tokens, reference_id, duration_ms, model)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (credential_id, harness, provider, tokens_used, input_tokens,
             output_tokens, reference_id, duration_ms, model),
        )
        row_id = cur.lastrowid
    if logger:
        logger.debug(
            f"Recorded usage: credential_id={credential_id} harness={harness} "
            f"tokens={tokens_used} model={model}"
        )
    return row_id


def get_usage_summary(
    conn: pymysql.Connection,
    credential_id: int,
    window_hours: int = 24,
) -> UsageSummary:
    """Aggregate usage for a single credential over a rolling time window."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(tokens_used), 0) AS total_tokens,
                   COUNT(*) AS call_count
            FROM usage_log
            WHERE credential_id = %s
              AND created_at >= NOW() - INTERVAL %s HOUR
            """,
            (credential_id, window_hours),
        )
        row = cur.fetchone()
    return UsageSummary(
        credential_id=credential_id,
        total_tokens=row["total_tokens"],
        call_count=row["call_count"],
    )


def get_usage_summaries(
    conn: pymysql.Connection,
    scope: str,
    window_hours: int = 24,
) -> list[UsageSummary]:
    """Usage summaries for all enabled credentials in a scope.

    Only rows with a ``credential_id`` are joined, so a credential-less run can never
    be counted against a credential's ``token_limit``. Use
    :func:`get_credentialless_usage` for that bucket.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id AS credential_id,
                   COALESCE(SUM(u.tokens_used), 0) AS total_tokens,
                   COUNT(u.id) AS call_count
            FROM credential c
            LEFT JOIN usage_log u
                   ON u.credential_id = c.id
                  AND u.created_at >= NOW() - INTERVAL %s HOUR
            WHERE c.scope = %s
              AND c.enabled = TRUE
            GROUP BY c.id
            """,
            (window_hours, scope),
        )
        rows = cur.fetchall()
    return [
        UsageSummary(
            credential_id=r["credential_id"],
            total_tokens=r["total_tokens"],
            call_count=r["call_count"],
        )
        for r in rows
    ]


def get_credentialless_usage(
    conn: pymysql.Connection,
    harness: str | None = None,
    window_hours: int = 24,
) -> list[CredentiallessUsage]:
    """Usage of runs that had no credential, grouped by ``(harness, provider)``.

    Kept separate from :func:`get_usage_summaries` so these rows are reported without
    ever being attributed to — or counted against the limit of — a credential. Grouped by
    BOTH axes because a harness can offer several credential-less providers, and a single
    lumped total could not tell them apart. Optionally filtered to one harness.
    """
    sql = (
        "SELECT harness, provider, COALESCE(SUM(tokens_used), 0) AS total_tokens, "
        "COUNT(*) AS call_count "
        "FROM usage_log WHERE credential_id IS NULL "
        "AND created_at >= NOW() - INTERVAL %s HOUR"
    )
    params: list = [window_hours]
    if harness is not None:
        sql += " AND harness = %s"
        params.append(harness)
    sql += " GROUP BY harness, provider ORDER BY harness, provider"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        CredentiallessUsage(
            harness=r["harness"],
            provider=r["provider"],
            total_tokens=r["total_tokens"],
            call_count=r["call_count"],
        )
        for r in rows
    ]
