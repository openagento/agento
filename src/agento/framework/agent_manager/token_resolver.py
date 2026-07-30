from __future__ import annotations

import random
import time

import pymysql

from .config import AgentManagerConfig
from .models import AgentProvider, Token
from .token_store import count_tokens_for_provider, select_token

# Contention retry budget, expressed as a wall-clock deadline rather than a
# fixed attempt count: what matters is how long the herd takes to drain, and
# that scales with DB latency (a loaded CI runner is far slower than a laptop).
# A fixed 20 x 10ms = 200ms cap failed spuriously once ~10 workers contended
# over a handful of rows.
_POOL_CONTENTION_BUDGET_SECONDS = 3.0
_POOL_CONTENTION_INITIAL_SLEEP_SECONDS = 0.01
_POOL_CONTENTION_MAX_SLEEP_SECONDS = 0.1


class TokenResolver:
    """Resolve which oauth_token to use for a given provider.

    Selection is LRU over the pool of healthy tokens (``status='ok'`` and
    unexpired). Sticky-primary semantics are gone — running jobs fan out over
    every enabled license so capacity is shared fairly.
    """

    def __init__(self, config: AgentManagerConfig | None = None) -> None:
        self._config = config or AgentManagerConfig()

    def resolve(self, conn: pymysql.Connection, agent_type: AgentProvider) -> Token:
        """Return the least-recently-used healthy token for ``agent_type``.

        Raises ``RuntimeError`` with an actionable message when no healthy
        token is available (distinguishes "none registered" vs
        "all errored/expired" so the operator knows whether to ``token:register``,
        ``token:refresh``, or ``token:reset``).
        """
        total = 0
        healthy = 0
        deadline = time.monotonic() + _POOL_CONTENTION_BUDGET_SECONDS
        sleep_for = _POOL_CONTENTION_INITIAL_SLEEP_SECONDS
        while True:
            token = select_token(conn, agent_type)
            if token is not None:
                return token

            total, healthy = count_tokens_for_provider(conn, agent_type)
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
                f"No enabled tokens for provider={agent_type.value}. "
                f"Register one: bin/agento token:register {agent_type.value} <label>"
            )
        if healthy > 0:
            raise RuntimeError(
                f"All {healthy} healthy tokens for provider={agent_type.value} are "
                "currently locked by concurrent workers; retry shortly."
            )
        raise RuntimeError(
            f"All {total} enabled tokens for provider={agent_type.value} are "
            f"unhealthy (errored or expired); {healthy} healthy. "
            f"Run 'bin/agento token:list --all' to inspect, then "
            f"'bin/agento token:refresh <id>' or 'bin/agento token:reset <id>'."
        )
