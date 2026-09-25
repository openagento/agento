"""Panel sessions behind the ``__Host-`` cookie (PRD E2 §4.2). Only hashes are stored."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime

from ..auth_context import resolve_auth_ttls
from ..toolbox_capability import token_hash
from .accounts import User, _in_transaction, authenticate_in

_SESSION_SQL = (
    "SELECT s.id, s.expires_at, u.id AS user_id, u.username, u.role, u.is_active"
    " FROM session s JOIN `user` u ON u.id = s.user_id"
    " WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > NOW() AND u.is_active = 1"
)


@dataclass(frozen=True)
class Session:
    id: str
    user: User
    expires_at: datetime  # naive UTC, as MySQL returns it


def _insert_session(cur, user: User, ttl: int) -> tuple[Session, str]:
    session_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
    cur.execute(
        "INSERT INTO session (id, token_hash, user_id, expires_at) VALUES (%s, %s, %s, NOW() + INTERVAL %s SECOND)",
        (session_id, token_hash(token), user.id, ttl),
    )
    cur.execute("SELECT expires_at FROM session WHERE id = %s", (session_id,))
    return Session(id=session_id, user=user, expires_at=cur.fetchone()["expires_at"]), token


def sign_in(conn, username: str, password: str) -> tuple[Session, str] | None:
    """Check the password and insert the session under the user row lock.

    A role change, deactivation or password change serializes with it: the session is either
    created first and revoked by that change, or refused.
    """
    ttl = resolve_auth_ttls(conn, None)["session_max_ttl"]

    def work(cur):
        user = authenticate_in(cur, username, password, lock=True)
        return _insert_session(cur, user, ttl) if user else None

    return _in_transaction(conn, work)


def create_session(conn, user: User) -> tuple[Session, str]:
    """A session for an already-authenticated user (tests, tooling); the raw token is never stored."""
    ttl = resolve_auth_ttls(conn, None)["session_max_ttl"]
    return _in_transaction(conn, lambda cur: _insert_session(cur, user, ttl))


def lookup_session(conn, token: str | None) -> Session | None:
    if not token:
        return None
    with conn.cursor() as cur:
        cur.execute(_SESSION_SQL, (token_hash(token),))
        row = cur.fetchone()
    if not row:
        return None
    user = User(id=row["user_id"], username=row["username"], role=row["role"], is_active=bool(row["is_active"]))
    return Session(id=row["id"], user=user, expires_at=row["expires_at"])


def revoke_session(conn, session_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE session SET revoked_at = NOW() WHERE id = %s AND revoked_at IS NULL", (session_id,))
    conn.commit()


def csrf_token(token: str) -> str:
    """Derived from the HttpOnly session token: a page that cannot read the cookie cannot compute it."""
    return hmac.new(token.encode("utf-8"), b"agento-csrf", hashlib.sha256).hexdigest()


def csrf_valid(token: str, presented: str | None) -> bool:
    return bool(presented) and hmac.compare_digest(csrf_token(token), presented)
