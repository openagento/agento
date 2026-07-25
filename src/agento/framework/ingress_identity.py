"""Ingress identity model — maps inbound identities to agent_view."""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from time import monotonic

import regex

logger = logging.getLogger(__name__)

# Regex-matched identity types are MODULE-OWNED: the framework registry starts EMPTY and is
# populated at bootstrap from each module's di.json `regex_identity_types` (see bootstrap.py).
# The owning module (e.g. outlook contributes "outlook_sender") drops its entry when disabled, so
# the framework never hardcodes a channel string. Every caller gates on is_regex_identity_type.
_REGEX_IDENTITY_TYPES: set[str] = set()

# Wall-clock budget for the regex matcher (SEC-F2 — the ReDoS bound). A per-pattern limit AND a
# total-per-lookup deadline; the whole lookup is bounded regardless of how many bindings exist.
_PER_PATTERN_S = 0.1
_LOOKUP_BUDGET_S = 0.5
# RFC 5321 caps an address at 320 chars; a longer sender is not a real address — skip matching.
_SENDER_MAX_LEN = 320

# Bounded rate-limiter for skip/timeout warnings: log at most once per binding id per window.
# Capped LRU so a flood of distinct bindings cannot grow it without bound (never poll-scoped —
# the matcher receives no poll state). Keyed by binding id; the raw pattern is NEVER logged.
_WARN_LRU_MAX = 256
_WARN_TTL_S = 60.0
_warn_seen: OrderedDict[int, float] = OrderedDict()


def _should_warn(binding_id: int) -> bool:
    """Return True at most once per binding id per _WARN_TTL_S, bounded to _WARN_LRU_MAX entries."""
    now = monotonic()
    last = _warn_seen.get(binding_id)
    if last is not None and (now - last) < _WARN_TTL_S:
        _warn_seen.move_to_end(binding_id)
        return False
    _warn_seen[binding_id] = now
    _warn_seen.move_to_end(binding_id)
    while len(_warn_seen) > _WARN_LRU_MAX:
        _warn_seen.popitem(last=False)
    return True


def register_regex_identity_type(identity_type: str) -> None:
    """Register an identity type whose bindings are matched by regex fullmatch (module-owned)."""
    _REGEX_IDENTITY_TYPES.add(identity_type)


def clear_regex_identity_types() -> None:
    """Reset the regex-type registry (called at the start of every bootstrap)."""
    _REGEX_IDENTITY_TYPES.clear()


def is_regex_identity_type(identity_type: str) -> bool:
    """True if bindings of this type are matched by regex fullmatch, False for exact match."""
    return identity_type in _REGEX_IDENTITY_TYPES


@dataclass
class IngressIdentity:
    id: int
    identity_type: str
    identity_value: str
    agent_view_id: int
    priority: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: dict) -> IngressIdentity:
        return cls(
            id=row["id"],
            identity_type=row["identity_type"],
            identity_value=row["identity_value"],
            agent_view_id=row["agent_view_id"],
            priority=int(row["priority"]),
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def get_ingress_identity(conn, identity_type: str, identity_value: str) -> IngressIdentity | None:
    """Look up an ingress identity by type and value."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM ingress_identity WHERE identity_type = %s AND identity_value = %s",
            (identity_type, identity_value),
        )
        row = cur.fetchone()
    return IngressIdentity.from_row(row) if row else None


def get_active_identities_for_type(conn, identity_type: str) -> list[IngressIdentity]:
    """All active bindings of a type, highest priority first (id ASC as a stable tie-break)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM ingress_identity WHERE identity_type = %s AND is_active = 1 "
            "ORDER BY priority DESC, id ASC",
            (identity_type,),
        )
        return [IngressIdentity.from_row(row) for row in cur.fetchall()]


def get_identities_for_agent_view(conn, agent_view_id: int) -> list[IngressIdentity]:
    """Get all ingress identities bound to a specific agent_view."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM ingress_identity WHERE agent_view_id = %s "
            "ORDER BY identity_type, priority DESC, identity_value, id",
            (agent_view_id,),
        )
        return [IngressIdentity.from_row(row) for row in cur.fetchall()]


def match_ingress_identities(conn, identity_type: str, identity_value: str) -> list[IngressIdentity]:
    """Return every ACTIVE binding of ``identity_type`` that matches ``identity_value``.

    Exact (non-regex) types: at most one row (the unique key), returned only when active.
    Regex types: each active binding's ``identity_value`` is a case-insensitive ``fullmatch``
    pattern. Matching is bounded (SEC-F2) by a dual wall-clock budget — a per-pattern limit AND a
    total-per-lookup deadline — using the ``regex`` engine's in-process ``timeout`` (the only
    reliable bound on catastrophic backtracking). A timed-out or invalid pattern is skipped (WARN
    by binding id, never the raw pattern; rate-limited); once the total budget is spent the
    remaining rows fail closed (treated as no-match). The result is always deterministic, so the
    caller's cursor discipline is unaffected.
    """
    if not is_regex_identity_type(identity_type):
        it = get_ingress_identity(conn, identity_type, identity_value)
        return [it] if it is not None and it.is_active else []

    if len(identity_value) > _SENDER_MAX_LEN:
        return []

    rows = get_active_identities_for_type(conn, identity_type)
    matches: list[IngressIdentity] = []
    deadline = monotonic() + _LOOKUP_BUDGET_S
    budget_spent = False
    for row in rows:
        remaining = deadline - monotonic()
        if remaining <= 0:
            budget_spent = True
            break
        try:
            if regex.fullmatch(
                row.identity_value, identity_value,
                regex.VERSION0 | regex.IGNORECASE,
                timeout=min(_PER_PATTERN_S, remaining),
            ):
                matches.append(row)
        except (regex.error, TimeoutError):
            if _should_warn(row.id):
                logger.warning(
                    "Skipping ingress binding id=%d type=%s: invalid or too-slow regex",
                    row.id, identity_type,
                )
    if budget_spent:
        logger.warning(
            "Ingress regex lookup for type=%s hit the per-lookup budget; remaining bindings "
            "failed closed (no match)", identity_type,
        )
    return matches


def bind_identity(
    conn, identity_type: str, identity_value: str, agent_view_id: int, priority: int | None = None
) -> None:
    """Bind an inbound identity to an agent_view. Upserts on (type, value).

    ``priority`` is preserved on upsert when None (defaults to 0 on first insert), and set when given.
    """
    with conn.cursor() as cur:
        if priority is None:
            cur.execute(
                """
                INSERT INTO ingress_identity (identity_type, identity_value, agent_view_id, is_active)
                VALUES (%s, %s, %s, 1)
                ON DUPLICATE KEY UPDATE agent_view_id = VALUES(agent_view_id), is_active = 1
                """,
                (identity_type, identity_value, agent_view_id),
            )
        else:
            cur.execute(
                """
                INSERT INTO ingress_identity (identity_type, identity_value, agent_view_id, priority, is_active)
                VALUES (%s, %s, %s, %s, 1)
                ON DUPLICATE KEY UPDATE agent_view_id = VALUES(agent_view_id),
                    priority = VALUES(priority), is_active = 1
                """,
                (identity_type, identity_value, agent_view_id, priority),
            )
    conn.commit()


def unbind_identity(conn, identity_type: str, identity_value: str) -> bool:
    """Remove an ingress identity binding. Returns True if a row was deleted."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM ingress_identity WHERE identity_type = %s AND identity_value = %s",
            (identity_type, identity_value),
        )
        deleted = cur.rowcount > 0
    conn.commit()
    return deleted


def list_identities(conn, *, identity_type: str | None = None) -> list[IngressIdentity]:
    """List all ingress identities, optionally filtered by type."""
    query = "SELECT * FROM ingress_identity"
    params: list = []
    if identity_type:
        query += " WHERE identity_type = %s"
        params.append(identity_type)
    query += " ORDER BY identity_type, priority DESC, identity_value, id"
    with conn.cursor() as cur:
        cur.execute(query, params)
        return [IngressIdentity.from_row(row) for row in cur.fetchall()]
