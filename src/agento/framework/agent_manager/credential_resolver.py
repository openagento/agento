from __future__ import annotations

import random
import time

import pymysql

from .config import AgentManagerConfig
from .credential_store import (
    RefreshLease,
    count_credentials_for_scope,
    earliest_lease_expiry_for_scope,
    select_credential,
)
from .errors import CredentialsBusyError
from .models import CredentialRecord

# Contention retry budget, expressed as a wall-clock deadline rather than a
# fixed attempt count: what matters is how long the herd takes to drain, and
# that scales with both DB latency and worker count (a loaded CI runner is far
# slower than a laptop). A fixed 20 x 10ms = 200ms cap failed spuriously once
# ~10 workers contended over a handful of rows.
#
# Sized for the documented ceiling of ~150 concurrent claimants (see
# tests/integration/test_token_selection_concurrency.py, which exercises 200):
# draining 200 workers over 3 credentials measures ~1s locally, and CI runs several
# times slower. The budget is a ceiling, not a cost — an uncontended claim
# returns on the first attempt and never sleeps. Raise it if you raise
# AGENTO_CONSUMER_MAX_WORKERS beyond that.
_POOL_CONTENTION_BUDGET_SECONDS = 15.0
_POOL_CONTENTION_INITIAL_SLEEP_SECONDS = 0.01
_POOL_CONTENTION_MAX_SLEEP_SECONDS = 0.1

# How close to its expiry a rotating credential must be before handing it to a run is
# treated as "this run will probably rotate it", which makes the row exclusive for the
# duration of that run. A HEURISTIC, not a proof: nothing bounds a job's wall clock
# (job_timeout_seconds bounds each CLI *subprocess*, and stale-job recovery skips any
# live pid), so a run handed a credential judged fresh can still cross expiry mid-run.
# The consequence of getting it wrong is a self-expiring throttle plus fail-over, not a
# quarantine — see the `rotated WITHOUT holding a refresh lease` ERROR, which measures
# exactly how often the heuristic is wrong.
_DEFAULT_REFRESH_HORIZON_SECONDS = 2100  # job_timeout 1200 + 900 slack

# Liveness deadline for a refresh lease — NOT an estimate of a job's duration. The owning
# consumer renews it from its own main-loop tick (and while draining on shutdown) for as
# long as it still has a worker for that job, so a lease of any wall-clock length is fine
# and a dead consumer's leases free themselves within one TTL with no reaper.
_DEFAULT_LEASE_TTL_SECONDS = 300

# A pool blocked by a refresh lease drains on a job's timescale, not a claim's, so the
# 15 s contention budget would burn out long before the holder finishes. Waiting that long
# is pointless too: the correct answer is to requeue the job and let another worker (or the
# same one later) find the pool free. Kept separate so ordinary claim contention keeps its
# fast, cheap retry.
_LEASE_CONTENTION_BUDGET_SECONDS = 2.0


class CredentialResolver:
    """Resolve which credential row to use for a given credential scope.

    Selection is LRU over the pool of healthy credentials (``status='ok'`` and
    unexpired). Sticky-primary semantics are gone — running jobs fan out over
    every enabled license so capacity is shared fairly.
    """

    def __init__(
        self,
        config: AgentManagerConfig | None = None,
        *,
        refresh_horizon_seconds: int = _DEFAULT_REFRESH_HORIZON_SECONDS,
        lease_ttl_seconds: int = _DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        self._config = config or AgentManagerConfig()
        self._refresh_horizon_seconds = int(refresh_horizon_seconds)
        self._lease_ttl_seconds = int(lease_ttl_seconds)

    def _refresh_imminent(self, credential: CredentialRecord) -> bool:
        """True when handing this row to a run could make that run rotate its single-use
        refresh token — in which case the row must be exclusive for that run.

        "Rotatable" is decided by the framework-owned flat ``refresh_token`` field, not by
        a harness-specific ``type == 'oauth'`` literal: API-key rows have nothing to
        rotate, so they stay fully shared. Remaining lifetime comes from the owning
        harness's ``WorkspaceAdapter.credential_ttl_seconds`` (only the harness knows its
        CLI's payload shape and unit), looked up through the registry — the scope's owner
        is exactly the harness that wrote those credentials. Unknown lifetime is
        conservative: exclusive. An adapter predating the method, a disabled harness
        module, or a raising hook all land in the same conservative branch.
        """
        if not (credential.credentials or {}).get("refresh_token"):
            return False
        try:
            from ..harness.registry import get_harness_for_scope

            owner = get_harness_for_scope(credential.scope)
            adapter = owner.adapter.workspace_adapter if owner is not None else None
            ttl = adapter.credential_ttl_seconds(credential) if adapter is not None else None
        except Exception:
            ttl = None
        return True if ttl is None else ttl <= self._refresh_horizon_seconds

    def resolve(
        self,
        conn: pymysql.Connection,
        scope: str,
        *,
        lease_owner: str | None = None,
    ) -> CredentialRecord:
        """Return the least-recently-used healthy credential for ``scope``.

        When ``lease_owner`` is given and the claimed row is refresh-imminent, the row is
        claimed EXCLUSIVELY for that owner (``credential.lease_owner`` then equals it, so
        the caller knows it must renew and release). Omitting ``lease_owner`` degrades to
        the previous shared-claim behaviour — callers that never spawn a CLI (usage
        recording, workspace builds) need no lease. Rows held by someone else's live lease
        are excluded for every caller regardless.

        Raises ``RuntimeError`` with an actionable message when no healthy
        credential is available (distinguishes "none registered" vs
        "all errored/expired" so the operator knows whether to ``credential:register``,
        ``credential:refresh``, or ``credential:reset``). When credentials ARE healthy but
        every one is transiently busy (locked by a concurrent worker or held by a refresh
        lease), raises ``CredentialsBusyError`` carrying ``pool_retry_at`` so the consumer
        waits for the pool to free up instead of dead-lettering the job.
        """
        lease = (
            RefreshLease(
                owner=lease_owner,
                ttl_seconds=self._lease_ttl_seconds,
                should_lease=self._refresh_imminent,
            )
            if lease_owner
            else None
        )
        total = 0
        healthy = 0
        budget = (
            _LEASE_CONTENTION_BUDGET_SECONDS
            if lease is not None
            else _POOL_CONTENTION_BUDGET_SECONDS
        )
        deadline = time.monotonic() + budget
        sleep_for = _POOL_CONTENTION_INITIAL_SLEEP_SECONDS
        while True:
            credential = select_credential(conn, scope, lease)
            if credential is not None:
                return credential

            total, healthy = count_credentials_for_scope(conn, scope)
            if total == 0 or healthy == 0:
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # End this transaction before sleeping. get_connection is autocommit=False at
            # REPEATABLE READ, so the snapshot taken by count_credentials_for_scope would
            # otherwise persist and every later plain read would replay it instead of
            # observing the holder's commit.
            conn.commit()
            # Jittered exponential backoff. Workers released at the same instant
            # (a consumer batch claiming jobs together) would otherwise retry in
            # lockstep and keep colliding on the same rows every round.
            time.sleep(min(sleep_for * (0.5 + random.random()), remaining))
            sleep_for = min(sleep_for * 2, _POOL_CONTENTION_MAX_SLEEP_SECONDS)

        if total == 0:
            raise RuntimeError(
                f"No enabled credentials for scope={scope}. "
                f"Register one: bin/agento credential:register {scope} <label>"
            )
        if healthy > 0:
            # Healthy tokens exist but all are transiently busy — a concurrent worker's
            # row lock or, in a single-token pool, the run rotating that token holding an
            # exclusive refresh lease. Don't dead-letter: tell the consumer when the
            # earliest lease frees the pool so it reschedules the job for after that
            # (``pool_retry_at``). ``None`` when the contention carries no lease (pure row
            # lock), leaving the consumer to fall back to ordinary backoff retry.
            raise CredentialsBusyError(
                f"All {healthy} healthy credentials for scope={scope} are "
                "currently locked by concurrent workers or held by a refresh lease "
                "(a run is rotating a near-expiry credential); retry shortly.",
                pool_retry_at=earliest_lease_expiry_for_scope(conn, scope),
            )
        raise RuntimeError(
            f"All {total} enabled credentials for scope={scope} are "
            f"unhealthy (errored or expired); {healthy} healthy. "
            f"Run 'bin/agento credential:list --all' to inspect, then "
            f"'bin/agento credential:refresh <id>' or 'bin/agento credential:reset <id>'."
        )
