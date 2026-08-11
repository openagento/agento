from __future__ import annotations

import random
import time

import pymysql

from .config import AgentManagerConfig
from .credential_store import count_credentials_for_scope, select_credential
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


class CredentialResolver:
    """Resolve which credential row to use for a given credential scope.

    Selection is LRU over the pool of healthy credentials (``status='ok'`` and
    unexpired). Sticky-primary semantics are gone — running jobs fan out over
    every enabled license so capacity is shared fairly.
    """

    def __init__(self, config: AgentManagerConfig | None = None) -> None:
        self._config = config or AgentManagerConfig()

    def resolve(self, conn: pymysql.Connection, scope: str) -> CredentialRecord:
        """Return the least-recently-used healthy credential for ``scope``.

        Raises ``RuntimeError`` with an actionable message when no healthy
        credential is available (distinguishes "none registered" vs
        "all errored/expired" so the operator knows whether to ``credential:register``,
        ``credential:refresh``, or ``credential:reset``).
        """
        total = 0
        healthy = 0
        deadline = time.monotonic() + _POOL_CONTENTION_BUDGET_SECONDS
        sleep_for = _POOL_CONTENTION_INITIAL_SLEEP_SECONDS
        while True:
            credential = select_credential(conn, scope)
            if credential is not None:
                return credential

            total, healthy = count_credentials_for_scope(conn, scope)
            if total == 0 or healthy == 0:
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
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
            raise RuntimeError(
                f"All {healthy} healthy credentials for scope={scope} are "
                "currently locked by concurrent workers; retry shortly."
            )
        raise RuntimeError(
            f"All {total} enabled credentials for scope={scope} are "
            f"unhealthy (errored or expired); {healthy} healthy. "
            f"Run 'bin/agento credential:list --all' to inspect, then "
            f"'bin/agento credential:refresh <id>' or 'bin/agento credential:reset <id>'."
        )
