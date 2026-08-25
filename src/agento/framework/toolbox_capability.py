"""Database-backed capability tokens for toolbox east-west authentication.

The toolbox derives every claim (agent_view_id, job_id, kind) from the row this
module writes, so a caller can never widen its own scope by editing a URL or a
request body. Only the SHA-256 hash of a token is stored: a database read cannot
recover a live credential.

``commit`` is a caller decision on every raw helper. Revocation must be able to land
in the SAME transaction as the job-status change that motivates it — a separate
commit would let a terminal status persist while the token stayed valid.

``rest_capability`` is the deliberate exception: it opens its OWN connection. The
toolbox validates the token on a different connection in a different process, so the
row has to be durable before the first request — the mint can never ride a caller's
transaction, and committing on a borrowed connection would publish whatever else that
caller had pending. It holds that connection only for the write: the mint closes it,
and the revoke opens a second one, so no block — however long — pins a pooled
connection it does not use.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets

from .database_config import DatabaseConfig
from .db import get_connection

KIND_MCP_JOB = "mcp_job"
KIND_MCP_INTERACTIVE = "mcp_interactive"
KIND_INTERNAL_REST = "internal_rest"

_KINDS = (KIND_MCP_JOB, KIND_MCP_INTERACTIVE, KIND_INTERNAL_REST)

TOKEN_BYTES = 32

MCP_CAPABILITY_TTL_SECONDS = 86400
INTERACTIVE_CAPABILITY_TTL_SECONDS = 43200
REST_CAPABILITY_TTL_SECONDS = 120


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_capability(
    conn,
    *,
    kind: str,
    agent_view_id: int | None,
    job_id: int | None = None,
    ttl_seconds: int,
    commit: bool = True,
) -> str:
    if kind not in _KINDS:
        raise ValueError(f"unknown capability kind: {kind!r}")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    # A viewless capability exists for one purpose: a default-scope config test. Only an
    # internal_rest one, owning no job; the toolbox accepts it on /config-test alone.
    if agent_view_id is None:
        if kind != KIND_INTERNAL_REST or job_id is not None:
            raise ValueError("only an internal_rest capability without a job may be viewless")
    elif not isinstance(agent_view_id, int) or isinstance(agent_view_id, bool) or agent_view_id <= 0:
        raise ValueError("every capability requires a positive agent_view_id")
    # bool is a subclass of int: True would pass an `isinstance(x, int)` check and become job 1.
    # Same per-kind invariants the Node verifier enforces — they must not drift apart.
    def _positive_id(v):
        return isinstance(v, int) and not isinstance(v, bool) and v > 0

    if kind == KIND_MCP_JOB:
        if not _positive_id(job_id):
            raise ValueError("mcp_job requires a positive integer job_id")
    elif kind == KIND_MCP_INTERACTIVE:
        if job_id is not None:
            raise ValueError("mcp_interactive must not carry a job_id")
    elif job_id is not None and not _positive_id(job_id):
        raise ValueError("internal_rest job_id must be null or a positive integer")

    token = secrets.token_urlsafe(TOKEN_BYTES)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO toolbox_capability "
            "(token_hash, kind, agent_view_id, job_id, expires_at) "
            "VALUES (%s, %s, %s, %s, DATE_ADD(NOW(), INTERVAL %s SECOND))",
            (token_hash(token), kind, agent_view_id, job_id, ttl_seconds),
        )
    if commit:
        conn.commit()
    return token


class CapabilityRevokeError(RuntimeError):
    """A capability outlived its block because the revoke failed.

    It carries the failure CATEGORY only. The driver exception it replaces embeds the
    DSN it just used — host, user, sometimes the password — and this error reaches an
    operator terminal and a log.
    """


@contextlib.contextmanager
def rest_capability(*, agent_view_id: int | None, ttl_seconds: int = REST_CAPABILITY_TTL_SECONDS,
                    db_config=None):
    """Mint an ``internal_rest`` capability whose revocation is guaranteed.

    Build the HTTP client INSIDE the ``with``: a constructor that raises (a rejected token,
    a malformed base URL) would otherwise leave a live bearer for the rest of its TTL, and
    a client whose ``close()`` fails would skip the revoke that follows it.

    Keep the block SHORT — one bounded request, or one verification attempt. The token
    expires after ``ttl_seconds`` (two minutes by default), so a block that waits for a
    human, retries with a backoff, or drives a whole agent run hands the work an already
    dead credential. Collect the input first, then mint; a retry mints again.

    Each database connection is this helper's own and lives only for its write — see the
    module docstring. Pass the ``db_config`` the caller already holds; it defaults to the
    environment, which is right inside the containers but is NOT the config a test or a
    second deployment uses.

    Revocation never masks the failure that reached it. A revoke that fails on the way out of
    a failing block is logged as a category and the original exception is re-raised; a revoke
    that fails on the success path is the only error there is, so it is raised — as a
    ``CapabilityRevokeError`` carrying the category, never the driver's own message.
    """
    resolved = db_config or DatabaseConfig.from_env()
    conn = get_connection(resolved)
    try:
        token = issue_capability(
            conn, kind=KIND_INTERNAL_REST, agent_view_id=agent_view_id, ttl_seconds=ttl_seconds
        )
    finally:
        conn.close()

    def _revoke():
        conn = get_connection(resolved)
        try:
            revoke_capability(conn, token)
        finally:
            conn.close()

    try:
        yield token
    except BaseException:
        try:
            _revoke()
        except Exception as revoke_error:  # the category only — a driver error carries the DSN
            logging.getLogger(__name__).warning(
                "could not revoke the internal_rest capability (%s)",
                type(revoke_error).__name__,
            )
        raise
    try:
        _revoke()
    except Exception as revoke_error:
        raise CapabilityRevokeError(
            "the internal_rest capability is still live: revoke failed "
            f"({type(revoke_error).__name__})"
        ) from None


def capability_client(build_client, *, agent_view_id: int, db_config=None,
                      ttl_seconds: int = REST_CAPABILITY_TTL_SECONDS):
    """Return a zero-argument context manager that opens ONE freshly-authorized client.

    Every ``with opener() as client`` mints its own ``internal_rest`` capability, builds the
    client from it, and revokes it on the way out. Nothing is shared between two uses, so a
    retry gets a live credential instead of the dead one the first attempt was given, and the
    time a caller spends between two requests — a human typing an API token, a backoff sleep —
    happens with no capability in existence at all.

    ``build_client`` takes the raw token and returns a closeable client::

        toolbox = capability_client(
            lambda token: ToolboxClient(url, capability_token=token),
            agent_view_id=view.id, db_config=db_config,
        )
        with toolbox() as client:
            client.do_one_bounded_thing()
    """
    @contextlib.contextmanager
    def _open():
        with rest_capability(
            agent_view_id=agent_view_id, ttl_seconds=ttl_seconds, db_config=db_config
        ) as token, contextlib.closing(build_client(token)) as client:
            yield client

    return _open


def revoke_job_capabilities(conn, job_id: int, *, commit: bool = True) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE toolbox_capability SET revoked_at = NOW() "
            "WHERE job_id = %s AND revoked_at IS NULL",
            (job_id,),
        )
        revoked = cur.rowcount
    if commit:
        conn.commit()
    return revoked


def revoke_capability(conn, token: str, *, commit: bool = True) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE toolbox_capability SET revoked_at = NOW() "
            "WHERE token_hash = %s AND revoked_at IS NULL",
            (token_hash(token),),
        )
        revoked = cur.rowcount
    if commit:
        conn.commit()
    return bool(revoked)


def purge_expired_capabilities(
    conn, *, older_than_seconds: int = 86400, commit: bool = True
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM toolbox_capability "
            "WHERE expires_at < DATE_SUB(NOW(), INTERVAL %s SECOND)",
            (older_than_seconds,),
        )
        deleted = cur.rowcount
    if commit:
        conn.commit()
    return deleted
