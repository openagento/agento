"""Observers for the core module."""
from __future__ import annotations

import logging

from agento.framework.database_config import DatabaseConfig
from agento.framework.db import get_connection, pooled

logger = logging.getLogger(__name__)

# One wake moves at most this many waiting jobs. A job past the batch wakes on its own
# ``scheduled_after`` (at most AUTH_WAIT_RETRY later).
WAKE_BATCH = 200
_MARKER = "CredentialsUnavailableError"


def auth_waiting_ids(cur) -> list[int]:
    """The lowest-id jobs that wait for a credential. A plain consistent read (no lock):
    every predicate is in ``idx_job_auth_wait (status, error_class, id)``, so it reads at
    most WAKE_BATCH index entries and never waits for another session's row locks."""
    cur.execute(
        "SELECT id FROM job WHERE status = 'TODO' AND error_class = %s ORDER BY id LIMIT %s",
        (_MARKER, WAKE_BATCH),
    )
    return [row["id"] for row in cur.fetchall()]


def wake_auth_waiting_jobs(conn) -> int:
    """Make the waiting jobs due now. Does not commit; returns the number of candidates.

    The UPDATE checks eligibility again, so a row that changed after the read does not
    match. It locks only the candidate ids, by primary key in ascending order, so two
    wakes at the same time serialize and do not deadlock (SCL-1). Credential scope is not
    filtered (it is agent_view config, which SQL cannot join): a job of another pool asks
    the resolver once and waits again.
    """
    with conn.cursor() as cur:
        ids = auth_waiting_ids(cur)
        if ids:
            cur.execute(
                "UPDATE job SET scheduled_after = LEAST(scheduled_after, NOW()) "
                f"WHERE id IN ({', '.join(['%s'] * len(ids))}) "
                "AND status = 'TODO' AND error_class = %s",
                (*ids, _MARKER),
            )
    return len(ids)


class WakeAuthWaitingJobsObserver:
    """On a new or refreshed credential, wake the jobs that wait for one.

    A schedule nudge, not a lifecycle change: the jobs stay TODO, so no event (EVT-1).
    The event payload carries the credential, so it is never logged (SEC-6).
    """

    def execute(self, event) -> None:
        try:
            with pooled(DatabaseConfig.from_env(), get_connection) as conn:
                woken = wake_auth_waiting_jobs(conn)
                conn.commit()
        except Exception as exc:
            logger.warning("core: auth-wait wake failed (%s)", type(exc).__name__)
            return
        if woken:
            logger.info("core: woke %d job(s) waiting for a credential", woken)


class RefreshCredentialLimitsObserver:
    """On a new or refreshed credential, read its limits now: the panel shows them right
    after a re-login, not on the next ``credential:limits`` tick.

    It re-reads the row by ``credential_id``; the payload's credentials are never used or
    logged (EVT-3, SEC-6). Telemetry, not a state change, so no event (EVT-1).
    """

    def execute(self, event) -> None:
        from agento.framework.cli.credential import refresh_credential_limits

        try:
            with pooled(DatabaseConfig.from_env(), get_connection) as conn:
                refresh_credential_limits(conn, logger, credential_id=event.credential_id)
        except Exception as exc:
            logger.warning(
                "core: limits read for credential id=%s failed (%s)",
                event.credential_id, type(exc).__name__,
            )
