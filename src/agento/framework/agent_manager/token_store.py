from __future__ import annotations

import logging
from datetime import UTC, datetime

import pymysql

from .models import AgentProvider, Token, encrypt_credentials


def register_token(
    conn: pymysql.Connection,
    agent_type: AgentProvider,
    label: str,
    credentials: dict,
    token_limit: int = 0,
    type: str = "oauth",
    logger: logging.Logger | None = None,
) -> Token:
    """Register or refresh a token. type defaults to 'oauth' to keep existing
    OAuth-only callers (interactive auth, capture_refreshed_credentials)
    unchanged. Resets status='ok' and clears any prior error_msg; pulls
    ``expires_at`` out of the credentials payload when present.

    Note: ``priority`` is intentionally NOT in the ON DUPLICATE KEY UPDATE list —
    re-registering an existing label (e.g. token:refresh) preserves any
    operator-set priority.
    """
    encrypted = encrypt_credentials(credentials)
    expires_at = _coerce_expires_at(credentials.get("expires_at"))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO oauth_token
                (agent_type, type, label, credentials, token_limit,
                 status, error_msg, expires_at)
            VALUES (%s, %s, %s, %s, %s, 'ok', NULL, %s)
            ON DUPLICATE KEY UPDATE
                type        = VALUES(type),
                credentials = VALUES(credentials),
                enabled     = TRUE,
                status      = 'ok',
                error_msg   = NULL,
                expires_at  = VALUES(expires_at),
                updated_at  = NOW()
            """,
            (agent_type.value, type, label, encrypted, token_limit, expires_at),
        )
        was_insert = bool(cur.lastrowid)
        if was_insert:
            token_id = cur.lastrowid
        else:
            cur.execute("SELECT id FROM oauth_token WHERE label = %s", (label,))
            token_id = cur.fetchone()["id"]
        cur.execute("SELECT * FROM oauth_token WHERE id = %s", (token_id,))
        row = cur.fetchone()
    action = "Registered" if was_insert else "Updated"
    if logger:
        logger.info(f"{action} token: id={token_id} label={label} type={type}")
    return Token.from_row(row)


def update_refreshed_credentials(
    conn: pymysql.Connection,
    token_id: int,
    credentials: dict,
    logger: logging.Logger | None = None,
) -> None:
    """Persist CLI-rotated credentials for an existing token *by id* WITHOUT
    resurrecting operator-controlled state. Unlike ``register_token`` this does
    NOT set ``enabled=TRUE``, reset ``status``, or clear ``error_msg``/``priority``
    — an operator who disabled or quarantined the token mid-run keeps that
    decision. ``expires_at`` is refreshed from the payload exactly as
    ``register_token`` does. Used by the per-provider
    ``capture_refreshed_credentials`` writers. Caller owns the transaction
    (the consumer hook commits)."""
    encrypted = encrypt_credentials(credentials)
    expires_at = _coerce_expires_at(credentials.get("expires_at"))
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE oauth_token
               SET credentials = %s,
                   expires_at  = %s,
                   updated_at  = NOW()
             WHERE id = %s
            """,
            (encrypted, expires_at, token_id),
        )
    if logger:
        logger.info("Captured rotated credentials for token id=%s", token_id)


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


def deregister_token(
    conn: pymysql.Connection,
    token_id: int,
    logger: logging.Logger | None = None,
) -> bool:
    """Soft-disable a token (sets enabled=FALSE). Returns True if found."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_token SET enabled = FALSE, updated_at = NOW() WHERE id = %s",
            (token_id,),
        )
        found = cur.rowcount > 0
    if logger:
        logger.info(f"Deregistered token: id={token_id} found={found}")
    return found


def list_tokens(
    conn: pymysql.Connection,
    agent_type: AgentProvider | None = None,
    enabled_only: bool = True,
) -> list[Token]:
    """List tokens, optionally filtered by agent_type and enabled status."""
    sql = "SELECT * FROM oauth_token WHERE 1=1"
    params: list = []
    if agent_type is not None:
        sql += " AND agent_type = %s"
        params.append(agent_type.value)
    if enabled_only:
        sql += " AND enabled = TRUE"
    sql += " ORDER BY id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [Token.from_row(r) for r in rows]


def get_token(
    conn: pymysql.Connection,
    token_id: int,
) -> Token | None:
    """Fetch a single token by ID. Returns None if not found."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM oauth_token WHERE id = %s", (token_id,))
        row = cur.fetchone()
    return Token.from_row(row) if row else None


def select_token(
    conn: pymysql.Connection,
    agent_type: AgentProvider,
) -> Token | None:
    """Claim the least-recently-used healthy token for ``agent_type`` and stamp
    ``used_at=UTC_TIMESTAMP(6)`` atomically.

    Filters: ``enabled=TRUE``, ``status='ok'``, ``expires_at`` either NULL or in
    the future (credential still valid), and ``throttled_until`` either NULL or in
    the past (not currently rate/usage-limited). Ordering: ``used_at`` ascending,
    NULLs first (never-used tokens win).

    Concurrency: the row is claimed with a plain ``FOR UPDATE`` (NOT
    ``SKIP LOCKED``) so a concurrent claimant *blocks* on the held row lock
    rather than skipping past it. When the holder commits its ``used_at`` bump,
    the waiter's scan resumes with a fresh current-read and picks a *different*
    (now higher-``used_at``) row — serializing claims so two workers never
    receive the same token. ``SKIP LOCKED`` cannot provide that guarantee here:
    the ``expires_at``/``throttled_until`` OR-predicates defeat the pool index,
    forcing a filesorted full scan over which ``SKIP LOCKED`` releases/skips
    locks and hands the same row to two concurrent claimants. Because the claim
    transaction commits immediately, the block is only microseconds long. Returns
    ``None`` when no healthy token exists; the caller raises with a diagnostic
    message.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM oauth_token
             WHERE agent_type = %s
               AND enabled = TRUE
               AND status = 'ok'
               AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
               AND (throttled_until IS NULL OR throttled_until <= UTC_TIMESTAMP())
             ORDER BY priority ASC,
                      used_at IS NULL DESC, used_at ASC, id ASC
             LIMIT 1
             FOR UPDATE
            """,
            (agent_type.value,),
        )
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return None
        token_id = row["id"]
        cur.execute(
            "UPDATE oauth_token SET used_at = UTC_TIMESTAMP(6) WHERE id = %s",
            (token_id,),
        )
        cur.execute("SELECT * FROM oauth_token WHERE id = %s", (token_id,))
        full_row = cur.fetchone()
    conn.commit()
    return Token.from_row(full_row)


def mark_token_error(
    conn: pymysql.Connection,
    token_id: int,
    message: str,
    logger: logging.Logger | None = None,
) -> bool:
    """Flag a token as unhealthy after an auth failure. Returns True if found."""
    truncated = (message or "")[:1000]
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_token SET status = 'error', error_msg = %s, updated_at = NOW() "
            "WHERE id = %s",
            (truncated, token_id),
        )
        found = cur.rowcount > 0
    if logger:
        logger.warning(f"Marked token as error: id={token_id} msg={truncated!r} found={found}")
    return found


def throttle_token(
    conn: pymysql.Connection,
    token_id: int,
    until: datetime,
    message: str,
    logger: logging.Logger | None = None,
) -> bool:
    """Temporarily remove a token from selection until ``until`` (naive UTC) after a
    session/usage/rate-limit hit, WITHOUT poisoning it.

    Sets ``throttled_until`` only — ``status`` stays ``'ok'`` and ``expires_at``
    (credential expiry) is untouched. ``select_token``/``count_tokens_for_provider``
    skip the token while ``throttled_until`` is in the future and auto-include it once
    it passes. ``message`` is logged, not stored (``error_msg`` pairs with
    ``status='error'``). Returns True if the row was found."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_token SET throttled_until = %s, updated_at = NOW() WHERE id = %s",
            (until, token_id),
        )
        found = cur.rowcount > 0
    if logger:
        logger.warning(
            f"Throttled token: id={token_id} until={until} msg={(message or '')[:200]!r} found={found}"
        )
    return found


def clear_token_error(
    conn: pymysql.Connection,
    token_id: int,
    logger: logging.Logger | None = None,
) -> bool:
    """Clear error status AND any usage-limit throttle on a token (operator recovery,
    e.g. ``token:reset``). Returns True if found."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_token SET status = 'ok', error_msg = NULL, throttled_until = NULL, "
            "updated_at = NOW() WHERE id = %s",
            (token_id,),
        )
        found = cur.rowcount > 0
    if logger:
        logger.info(f"Cleared token error: id={token_id} found={found}")
    return found


def set_token_priority(
    conn: pymysql.Connection,
    token_id: int,
    priority: int,
    logger: logging.Logger | None = None,
) -> bool:
    """Set the selection priority for a token. Lower wins (default 0)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_token SET priority = %s, updated_at = NOW() WHERE id = %s",
            (int(priority), int(token_id)),
        )
        found = cur.rowcount > 0
    if logger:
        logger.info(f"Set token priority: id={token_id} priority={priority} found={found}")
    return found


def count_tokens_for_provider(
    conn: pymysql.Connection,
    agent_type: AgentProvider,
) -> tuple[int, int]:
    """Return (enabled_total, healthy) counts, where ``healthy`` mirrors
    ``select_token``'s eligibility (status='ok', unexpired, and not currently
    throttled) — used for diagnostic messages when ``select_token`` returns None and
    to decide usage-limit/auth failover (``retry_with_other_token``)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM oauth_token WHERE agent_type = %s AND enabled = TRUE",
            (agent_type.value,),
        )
        total = cur.fetchone()["c"]
        cur.execute(
            """
            SELECT COUNT(*) AS c FROM oauth_token
             WHERE agent_type = %s AND enabled = TRUE AND status = 'ok'
               AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
               AND (throttled_until IS NULL OR throttled_until <= UTC_TIMESTAMP())
            """,
            (agent_type.value,),
        )
        healthy = cur.fetchone()["c"]
    return int(total), int(healthy)
