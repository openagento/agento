"""Artifact launches on the apps origin (PRD E2 §4.3). Only hashes are stored.

A launch pins ``(artifact_code, version_id)`` and a scope. The panel gets a one-time exchange
code; redeeming it (one POST, 30 s) sets the per-launch cookie that ``authorize_files`` checks
on every file request. E2 writes the no-manifest constants: E6 owns the manifest.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime

from ..auth_context import resolve_auth_ttls
from ..toolbox_capability import token_hash
from .accounts import AccessError, User, _in_transaction, _lock_users, has_operation

NO_MANIFEST_FINGERPRINT = hashlib.sha256(b"").hexdigest()
EXCHANGE_TTL_SECONDS = 30
MAX_CONCURRENT_CEILING = 20
_MAX_CONCURRENT_PATH = "web/launch/max_concurrent"

_LIVE = "l.revoked_at IS NULL AND l.expires_at > NOW()"
_COLUMNS = "l.id, l.user_id, l.artifact_code, l.version_id, l.workspace_id, l.agent_view_id, l.expires_at"


class AccessConfigError(ValueError):
    """``web/launch/max_concurrent`` does not resolve to a usable value."""


@dataclass(frozen=True)
class Launch:
    id: str
    user_id: int
    artifact_code: str
    version_id: str
    workspace_id: int
    agent_view_id: int | None
    expires_at: datetime  # naive UTC, as MySQL returns it


def _launch(row: dict) -> Launch:
    return Launch(**{k: row[k] for k in Launch.__dataclass_fields__})


def max_concurrent(conn, workspace_id: int) -> int:
    """ENV -> DB (workspace, then default) -> the ``web`` module's ``config.json``.

    web never runs ``bootstrap()``, so the generic resolver has no manifests there; the
    module's ``config.json`` is read from disk instead.
    """
    from ..config_resolver import path_to_env_key, read_config_defaults
    from ..core_config import _find_module_dir
    from ..scoped_config import Scope, load_scoped_db_overrides

    raw = os.environ.get(path_to_env_key(_MAX_CONCURRENT_PATH))
    if raw is None:
        for scope, scope_id in ((Scope.WORKSPACE, workspace_id), (Scope.DEFAULT, 0)):
            row = load_scoped_db_overrides(conn, scope, scope_id, strict=True).get(_MAX_CONCURRENT_PATH)
            if row is not None:
                raw = row[0]
                break
    if raw is None:
        module_dir = _find_module_dir("web")
        raw = read_config_defaults(module_dir).get("launch/max_concurrent") if module_dir else None
    text = str(raw).strip() if raw is not None and not isinstance(raw, bool) else ""
    if not text.isdigit() or int(text) < 1:
        raise AccessConfigError(f"{_MAX_CONCURRENT_PATH} must be a positive integer")
    return min(int(text), MAX_CONCURRENT_CEILING)


def create_launch(conn, user: User, *, workspace_id: int, agent_view_id: int | None,
                  artifact_code: str, version_id: str) -> tuple[Launch, str]:
    """Return the launch and its raw exchange code; the code is never stored."""
    ttl = resolve_auth_ttls(conn, workspace_id)["launch_max_ttl"]
    cap = max_concurrent(conn, workspace_id)
    launch_id, code = secrets.token_hex(16), secrets.token_urlsafe(32)

    def work(cur):
        # The lock serializes this user's creates (the cap holds) and orders this launch
        # against set_role / set_active / remove_grant, which lock the same row.
        row = _lock_users(cur, "id = %s", (user.id,)).get(user.id)
        if not row or not row["is_active"]:
            raise AccessError("not allowed")
        # The authorization that counts: inside the locked transaction, with the stored role.
        if not has_operation(conn, row["role"], "artifact.launch", workspace_id, agent_view_id):
            raise AccessError("not allowed")
        cur.execute(
            "INSERT INTO launch (id, token_hash, user_id, artifact_code, version_id, manifest_fingerprint,"
            " allowed_actions, workspace_id, agent_view_id, expires_at, exchange_code_hash, exchange_expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() + INTERVAL %s SECOND, %s,"
            " NOW() + INTERVAL %s SECOND)",
            (launch_id, token_hash(secrets.token_urlsafe(32)), user.id, artifact_code, version_id,
             NO_MANIFEST_FINGERPRINT, json.dumps([]), workspace_id, agent_view_id, ttl, token_hash(code),
             EXCHANGE_TTL_SECONDS),
        )
        # Oldest first beyond the cap; the new launch always survives.
        # ponytail: created_at has 1 s precision, so launches made in the same second are
        # ordered by id (random); add a sequence column if that ever matters.
        cur.execute(
            f"SELECT l.id FROM launch l WHERE l.user_id = %s AND {_LIVE}"
            " ORDER BY l.created_at DESC, l.id = %s DESC, l.id DESC LIMIT 1000 OFFSET %s",
            (user.id, launch_id, cap),
        )
        evict = [r["id"] for r in cur.fetchall()]
        if evict:
            cur.execute(
                "UPDATE launch SET revoked_at = NOW() WHERE id IN (" + ",".join(["%s"] * len(evict)) + ")",
                tuple(evict),
            )
        cur.execute(f"SELECT {_COLUMNS} FROM launch l WHERE l.id = %s", (launch_id,))
        return _launch(cur.fetchone())

    return _in_transaction(conn, work), code


def redeem(conn, launch_id: str, code: str) -> tuple[Launch, str] | None:
    """Single use: the one UPDATE that matches wins; the token is returned only to it."""
    if not isinstance(launch_id, str) or not isinstance(code, str) or not code:
        return None
    token = secrets.token_urlsafe(32)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE launch l JOIN `user` u ON u.id = l.user_id"
            " SET l.exchange_redeemed_at = NOW(), l.token_hash = %s"
            " WHERE l.id = %s AND l.exchange_code_hash = %s AND l.exchange_redeemed_at IS NULL"
            f" AND l.exchange_expires_at > NOW() AND {_LIVE} AND u.is_active = 1",
            (token_hash(token), launch_id, token_hash(code)),
        )
        won = cur.rowcount == 1
        row = None
        if won:
            cur.execute(f"SELECT {_COLUMNS} FROM launch l WHERE l.id = %s", (launch_id,))
            row = cur.fetchone()
    conn.commit()
    return (_launch(row), token) if row else None


def authorize_files(conn, tokens: list[str], artifact_code: str, version_id: str) -> bool:
    tokens = [t for t in tokens if t][:MAX_CONCURRENT_CEILING]
    if not tokens:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM launch l JOIN `user` u ON u.id = l.user_id"
            " WHERE l.token_hash IN (" + ",".join(["%s"] * len(tokens)) + ")"
            " AND l.artifact_code = %s AND l.version_id = %s"
            f" AND l.exchange_redeemed_at IS NOT NULL AND {_LIVE} AND u.is_active = 1 LIMIT 1",
            (*(token_hash(t) for t in tokens), artifact_code, version_id),
        )
        return cur.fetchone() is not None


def live_launch_ids(conn, launch_ids: list[str]) -> set[str]:
    launch_ids = list(launch_ids)[:MAX_CONCURRENT_CEILING]
    if not launch_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT l.id FROM launch l WHERE l.id IN (" + ",".join(["%s"] * len(launch_ids)) + f") AND {_LIVE}",
            tuple(launch_ids),
        )
        return {r["id"] for r in cur.fetchall()}


def revoke_launch(conn, user: User, launch_id: str) -> bool:
    """End one launch: the user's own; an admin may end any. ``user`` comes from this request's session lookup."""
    own, params = ("", (launch_id,)) if user.role == "admin" else (" AND l.user_id = %s", (launch_id, user.id))
    with conn.cursor() as cur:
        cur.execute(f"UPDATE launch l SET l.revoked_at = NOW() WHERE l.id = %s AND {_LIVE}{own}", params)
        done = cur.rowcount == 1
    conn.commit()
    return done


def list_launches(conn, user: User) -> list[Launch]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM launch l WHERE l.user_id = %s AND {_LIVE} ORDER BY l.created_at DESC, l.id",
            (user.id,),
        )
        return [_launch(r) for r in cur.fetchall()]
