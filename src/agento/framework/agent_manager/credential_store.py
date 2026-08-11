from __future__ import annotations

import logging
from datetime import UTC, datetime

import pymysql

from .models import CredentialRecord, encrypt_credentials


def register_credential(
    conn: pymysql.Connection,
    scope: str,
    label: str,
    credentials: dict,
    token_limit: int = 0,
    type: str = "oauth",
    logger: logging.Logger | None = None,
) -> CredentialRecord:
    """Register or refresh a token. type defaults to 'oauth' to keep existing
    OAuth-only callers (interactive auth, capture_refreshed_credentials)
    unchanged. Resets status='ok' and clears any prior error_msg; pulls
    ``expires_at`` out of the credentials payload when present.

    Note: ``priority`` is intentionally NOT in the ON DUPLICATE KEY UPDATE list —
    re-registering an existing label (e.g. credential:refresh) preserves any
    operator-set priority.
    """
    encrypted = encrypt_credentials(credentials)
    expires_at = _coerce_expires_at(credentials.get("expires_at"))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO credential
                (scope, agent_type, type, label, credentials, token_limit,
                 status, error_msg, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'ok', NULL, %s)
            ON DUPLICATE KEY UPDATE
                type        = VALUES(type),
                credentials = VALUES(credentials),
                enabled     = TRUE,
                status      = 'ok',
                error_msg   = NULL,
                expires_at  = VALUES(expires_at),
                updated_at  = NOW()
            """,
            (scope, scope, type, label, encrypted, token_limit, expires_at),
        )
        was_insert = bool(cur.lastrowid)
        if was_insert:
            credential_id = cur.lastrowid
        else:
            # Keyed on (scope, label): the label alone is not unique — the same label
            # in another scope is a DIFFERENT credential.
            cur.execute(
                "SELECT id FROM credential WHERE scope = %s AND label = %s",
                (scope, label),
            )
            credential_id = cur.fetchone()["id"]
        cur.execute("SELECT * FROM credential WHERE id = %s", (credential_id,))
        row = cur.fetchone()
    action = "Registered" if was_insert else "Updated"
    if logger:
        logger.info(f"{action} credential: id={credential_id} label={label} type={type}")
    return CredentialRecord.from_row(row)


def update_refreshed_credentials(
    conn: pymysql.Connection,
    credential_id: int,
    credentials: dict,
    logger: logging.Logger | None = None,
) -> None:
    """Persist CLI-rotated credentials for an existing credential *by id* WITHOUT
    resurrecting operator-controlled state. Unlike ``register_credential`` this does
    NOT set ``enabled=TRUE``, reset ``status``, or clear ``error_msg``/``priority``
    — an operator who disabled or quarantined the credential mid-run keeps that
    decision. ``expires_at`` is refreshed from the payload exactly as
    ``register_credential`` does. Used by the per-provider
    ``capture_refreshed_credentials`` writers. Caller owns the transaction
    (the consumer hook commits)."""
    encrypted = encrypt_credentials(credentials)
    expires_at = _coerce_expires_at(credentials.get("expires_at"))
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE credential
               SET credentials = %s,
                   expires_at  = %s,
                   updated_at  = NOW()
             WHERE id = %s
            """,
            (encrypted, expires_at, credential_id),
        )
    if logger:
        logger.info("Captured rotated credentials for credential id=%s", credential_id)


def _coerce_expires_at(value) -> datetime | None:
    """Convert credentials' ``expires_at`` (epoch seconds or ISO-8601) to a
    naive-UTC datetime; returns None on anything unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        if value.isdigit():
            try:
                return datetime.fromtimestamp(int(value), tz=UTC).replace(tzinfo=None)
            except (OverflowError, OSError, ValueError):
                return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            return None
    return None


def deregister_credential(
    conn: pymysql.Connection,
    credential_id: int,
    logger: logging.Logger | None = None,
) -> bool:
    """Soft-disable a credential (sets enabled=FALSE). Returns True if found."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE credential SET enabled = FALSE, updated_at = NOW() WHERE id = %s",
            (credential_id,),
        )
        found = cur.rowcount > 0
    if logger:
        logger.info(f"Deregistered credential: id={credential_id} found={found}")
    return found


def list_credentials(
    conn: pymysql.Connection,
    scope: str | None = None,
    enabled_only: bool = True,
) -> list[CredentialRecord]:
    """List credentials, optionally filtered by scope and enabled status."""
    sql = "SELECT * FROM credential WHERE 1=1"
    params: list = []
    if scope is not None:
        sql += " AND scope = %s"
        params.append(scope)
    if enabled_only:
        sql += " AND enabled = TRUE"
    sql += " ORDER BY id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [CredentialRecord.from_row(r) for r in rows]


def get_credential(
    conn: pymysql.Connection,
    credential_id: int,
) -> CredentialRecord | None:
    """Fetch a single credential by ID. Returns None if not found."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM credential WHERE id = %s", (credential_id,))
        row = cur.fetchone()
    return CredentialRecord.from_row(row) if row else None


def select_credential(
    conn: pymysql.Connection,
    scope: str,
) -> CredentialRecord | None:
    """Claim the least-recently-used healthy credential for ``scope`` and stamp
    ``used_at=UTC_TIMESTAMP(6)`` atomically.

    Filters: ``enabled=TRUE``, ``status='ok'``, ``expires_at`` either NULL or in
    the future (credential still valid), and ``throttled_until`` either NULL or in
    the past (not currently rate/usage-limited). Ordering: ``used_at`` ascending,
    NULLs first (never-used credentials win).

    Concurrency: the row is claimed with a plain ``FOR UPDATE`` (NOT
    ``SKIP LOCKED``) so a concurrent claimant *blocks* on the held row lock
    rather than skipping past it. When the holder commits its ``used_at`` bump,
    the waiter's scan resumes with a fresh current-read and picks a *different*
    (now higher-``used_at``) row — serializing claims so two workers never
    receive the same credential. ``SKIP LOCKED`` cannot provide that guarantee here:
    the ``expires_at``/``throttled_until`` OR-predicates defeat the pool index,
    forcing a filesorted full scan over which ``SKIP LOCKED`` releases/skips
    locks and hands the same row to two concurrent claimants. Because the claim
    transaction commits immediately, the block is only microseconds long. Returns
    ``None`` when no healthy credential exists; the caller raises with a diagnostic
    message.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM credential
             WHERE scope = %s
               AND enabled = TRUE
               AND status = 'ok'
               AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
               AND (throttled_until IS NULL OR throttled_until <= UTC_TIMESTAMP())
             ORDER BY priority ASC,
                      used_at IS NULL DESC, used_at ASC, id ASC
             LIMIT 1
             FOR UPDATE
            """,
            (scope,),
        )
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return None
        credential_id = row["id"]
        cur.execute(
            "UPDATE credential SET used_at = UTC_TIMESTAMP(6) WHERE id = %s",
            (credential_id,),
        )
        cur.execute("SELECT * FROM credential WHERE id = %s", (credential_id,))
        full_row = cur.fetchone()
    conn.commit()
    return CredentialRecord.from_row(full_row)


def mark_credential_error(
    conn: pymysql.Connection,
    credential_id: int,
    message: str,
    logger: logging.Logger | None = None,
) -> bool:
    """Flag a credential as unhealthy after an auth failure. Returns True if found."""
    truncated = (message or "")[:1000]
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE credential SET status = 'error', error_msg = %s, updated_at = NOW() "
            "WHERE id = %s",
            (truncated, credential_id),
        )
        found = cur.rowcount > 0
    if logger:
        # The message is derived from CLI output (the harness's error classifier reads
        # stderr), so it can carry prompt or customer content. It is stored in
        # `credential.error_msg` for operators; the log records only that it happened.
        logger.warning(
            f"Marked credential as error: id={credential_id} "
            f"msg_len={len(truncated or '')} found={found} "
            f"(reason in credential.error_msg)"
        )
    return found


def throttle_credential(
    conn: pymysql.Connection,
    credential_id: int,
    until: datetime,
    message: str,
    logger: logging.Logger | None = None,
) -> bool:
    """Temporarily remove a credential from selection until ``until`` (naive UTC) after a
    session/usage/rate-limit hit, WITHOUT poisoning it.

    Sets ``throttled_until`` only — ``status`` stays ``'ok'`` and ``expires_at``
    (credential expiry) is untouched. ``select_credential``/``count_credentials_for_scope``
    skip the token while ``throttled_until`` is in the future and auto-include it once
    it passes. ``message`` is logged, not stored (``error_msg`` pairs with
    ``status='error'``). Returns True if the row was found."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE credential SET throttled_until = %s, updated_at = NOW() WHERE id = %s",
            (until, credential_id),
        )
        found = cur.rowcount > 0
    if logger:
        logger.warning(
            f"Throttled credential: id={credential_id} until={until} "
            f"msg_len={len(message or '')} found={found}"
        )
    return found


def clear_credential_error(
    conn: pymysql.Connection,
    credential_id: int,
    logger: logging.Logger | None = None,
) -> bool:
    """Clear error status AND any usage-limit throttle on a token (operator recovery,
    e.g. ``credential:reset``). Returns True if found."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE credential SET status = 'ok', error_msg = NULL, throttled_until = NULL, "
            "updated_at = NOW() WHERE id = %s",
            (credential_id,),
        )
        found = cur.rowcount > 0
    if logger:
        logger.info(f"Cleared token error: id={credential_id} found={found}")
    return found


def set_credential_priority(
    conn: pymysql.Connection,
    credential_id: int,
    priority: int,
    logger: logging.Logger | None = None,
) -> bool:
    """Set the selection priority for a token. Lower wins (default 0)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE credential SET priority = %s, updated_at = NOW() WHERE id = %s",
            (int(priority), int(credential_id)),
        )
        found = cur.rowcount > 0
    if logger:
        logger.info(f"Set token priority: id={credential_id} priority={priority} found={found}")
    return found


def count_credentials_for_scope(
    conn: pymysql.Connection,
    scope: str,
) -> tuple[int, int]:
    """Return (enabled_total, healthy) counts, where ``healthy`` mirrors
    ``select_credential``'s eligibility (status='ok', unexpired, and not currently
    throttled) — used for diagnostic messages when ``select_credential`` returns None and
    to decide usage-limit/auth failover (``retry_with_other_token``)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM credential WHERE scope = %s AND enabled = TRUE",
            (scope,),
        )
        total = cur.fetchone()["c"]
        cur.execute(
            """
            SELECT COUNT(*) AS c FROM credential
             WHERE scope = %s AND enabled = TRUE AND status = 'ok'
               AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
               AND (throttled_until IS NULL OR throttled_until <= UTC_TIMESTAMP())
            """,
            (scope,),
        )
        healthy = cur.fetchone()["c"]
    return int(total), int(healthy)
