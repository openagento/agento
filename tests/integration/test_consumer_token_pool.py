"""Integration: auth failure poisons the offending token and the job retries
onto the next healthy token in the LRU pool, dead-lettering only once the pool
is exhausted (real MySQL)."""
from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from unittest.mock import patch

from agento.framework.agent_manager.errors import AuthenticationError, UsageLimitError
from agento.framework.agent_manager.models import encrypt_credentials
from agento.framework.consumer import Consumer
from agento.framework.runner import RunResult
from agento.modules.claude.src.runner import TokenClaudeRunner
from agento.modules.codex.src.runner import TokenCodexRunner

from .conftest import _test_connection, fetch_job, insert_queued_job, update_job


def _seed_token(label: str, *, priority: int, agent_type: str = "claude") -> int:
    """Insert an enabled, healthy token with an explicit priority. Lower
    priority wins selection (``ORDER BY priority ASC``)."""
    encrypted = encrypt_credentials({"subscription_key": f"sk-invalid-{label}"})
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oauth_token
                    (agent_type, type, label, credentials, enabled, status, priority)
                VALUES (%s, 'oauth', %s, %s, TRUE, 'ok', %s)
                """,
                (agent_type, label, encrypted, priority),
            )
            return cur.lastrowid
    finally:
        conn.close()


def _bind_provider(agent_type: str = "claude") -> None:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO core_config_data (scope, scope_id, path, value, encrypted)
                VALUES ('default', 0, 'agent_view/provider', %s, 0)
                ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = NOW()
                """,
                (agent_type,),
            )
    finally:
        conn.close()


def _token_status(token_id: int) -> str:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM oauth_token WHERE id = %s", (token_id,))
            return cur.fetchone()["status"]
    finally:
        conn.close()


def _token_throttle(token_id: int):
    """Return the token's throttled_until (naive datetime or None)."""
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT throttled_until FROM oauth_token WHERE id = %s", (token_id,))
            return cur.fetchone()["throttled_until"]
    finally:
        conn.close()


def test_auth_failure_retries_onto_next_healthy_token_then_dead_when_exhausted(
    int_db_config, int_consumer_config,
):
    """Two invalid tokens, A (priority 0) then B (priority 1). Each attempt the
    consumer resolves the lowest-priority healthy token; the runner rejects the
    credential with a 401; the consumer poisons that token and — because a
    healthy alternative still exists — requeues the job. Once both are poisoned
    the pool is exhausted and the job dead-letters."""
    logger = logging.getLogger("test")
    _bind_provider("claude")
    token_a = _seed_token("a", priority=0)
    token_b = _seed_token("b", priority=1)
    job_id = insert_queued_job(
        reference_id="AI-AUTH", idempotency_key="auth-pool:1", max_attempts=3,
    )

    # The runner rejects whatever credential it is handed. token_id is omitted
    # so the consumer attributes the failure to the token IT resolved from the
    # pool (``_handle_auth_failure`` falls back to ``token.id``).
    def _reject(self, *args, **kwargs):
        raise AuthenticationError("401 Unauthorized")

    # Attempt 1: token A selected (priority 0) -> poisoned -> requeue (B healthy).
    with patch.object(TokenClaudeRunner, "run", new=_reject):
        consumer = Consumer(int_db_config, int_consumer_config, logger)
        job = consumer._try_dequeue()
        assert job is not None
        consumer._execute_job(job)

    row = fetch_job(job_id)
    assert row["status"] == "TODO"
    assert row["attempt"] == 1
    assert row["error_class"] == "AuthenticationError"
    assert _token_status(token_a) == "error"
    assert _token_status(token_b) == "ok"

    # Unblock the retry backoff.
    update_job(job_id, scheduled_after="2000-01-01 00:00:00")

    # Attempt 2: token B selected (A poisoned) -> poisoned -> pool exhausted -> DEAD.
    with patch.object(TokenClaudeRunner, "run", new=_reject):
        consumer2 = Consumer(int_db_config, int_consumer_config, logger)
        job2 = consumer2._try_dequeue()
        assert job2 is not None
        assert job2.id == job_id
        consumer2._execute_job(job2)

    row = fetch_job(job_id)
    assert row["status"] == "DEAD"
    assert row["attempt"] == 2
    assert _token_status(token_a) == "error"
    assert _token_status(token_b) == "error"


def _usage_limit_failover(
    db_config, consumer_config, runner_cls, agent_type, success_result, cheap_model,
):
    """Shared body: a usage/session limit on the priority-0 token must THROTTLE it
    (cooldown, not poison) and fail the job over to the healthy priority-1 token,
    which then succeeds. Verified against real MySQL for one provider.

    ``cheap_model`` is threaded through as ``model_override`` (a cheap model per the
    task's cost intent); the runner is mocked so no real API call is made."""
    logger = logging.getLogger("test")
    _bind_provider(agent_type)
    token_a = _seed_token("a", priority=0, agent_type=agent_type)
    token_b = _seed_token("b", priority=1, agent_type=agent_type)
    job_id = insert_queued_job(
        reference_id=f"AI-LIMIT-{agent_type}",
        idempotency_key=f"limit-pool:{agent_type}:1",
        max_attempts=3,
    )

    def _limited(self, *args, **kwargs):
        # No token_id → consumer attributes it to the token it resolved (token A).
        raise UsageLimitError("You've hit your session limit")

    # Attempt 1: token A selected (priority 0) -> usage limit -> throttled -> requeue (B healthy).
    with patch.object(runner_cls, "run", new=_limited):
        consumer = Consumer(db_config, consumer_config, logger, model_override=cheap_model)
        job = consumer._try_dequeue()
        assert job is not None
        consumer._execute_job(job)

    row = fetch_job(job_id)
    assert row["status"] == "TODO"
    assert row["attempt"] == 1
    assert row["error_class"] == "UsageLimitError"
    # Throttled, NOT poisoned: status stays 'ok' and a future throttled_until is set.
    assert _token_status(token_a) == "ok"
    assert _token_throttle(token_a) is not None
    assert _token_throttle(token_a) > datetime.now(UTC).replace(tzinfo=None)
    assert _token_status(token_b) == "ok"
    assert _token_throttle(token_b) is None

    # Unblock the retry backoff.
    update_job(job_id, scheduled_after="2000-01-01 00:00:00")

    # Attempt 2: token A still throttled -> token B selected -> run succeeds -> SUCCESS.
    with patch.object(runner_cls, "run", return_value=success_result):
        consumer2 = Consumer(db_config, consumer_config, logger, model_override=cheap_model)
        job2 = consumer2._try_dequeue()
        assert job2 is not None
        assert job2.id == job_id
        consumer2._execute_job(job2)

    row = fetch_job(job_id)
    assert row["status"] == "SUCCESS"
    assert row["attempt"] == 2
    # A stayed a healthy (throttled) token the whole time; it auto-recovers after cooldown.
    assert _token_status(token_a) == "ok"


def test_claude_usage_limit_throttles_and_fails_over(int_db_config, int_consumer_config):
    success = RunResult(
        raw_output="ok", input_tokens=1500, output_tokens=800, cost_usd=0.01,
        num_turns=1, duration_ms=1000, subtype="success", agent_type="claude",
    )
    _usage_limit_failover(
        int_db_config, int_consumer_config, TokenClaudeRunner, "claude", success,
        cheap_model="claude-haiku-4-5-20251001",
    )


def test_codex_usage_limit_throttles_and_fails_over(int_db_config, int_consumer_config):
    success = RunResult(
        raw_output="ok", input_tokens=1000, output_tokens=None, num_turns=1,
        duration_ms=1000, subtype="success", agent_type="codex",
    )
    _usage_limit_failover(
        int_db_config, int_consumer_config, TokenCodexRunner, "codex", success,
        cheap_model="gpt-5.4-mini",
    )


# --- Transient auth (revoked / stale access token) ---------------------------
# NOTE: unlike the two tests above (which patch ``run`` and raise directly, so they
# never reach the parser), these patch the SUBPROCESS seam. Everything above it runs
# for real: _extract_raw -> _parse_output -> parse_claude_output -> _classify_error ->
# the consumer's except clause -> _handle_transient_auth -> retry_policy -> MySQL.

# The raw stream-json a real `claude -p --output-format stream-json` run emits when the
# stored OAuth credential is rejected.
_REVOKED_RAW = (
    '{"type":"system","subtype":"init","session_id":"sess-revoked","mcp_servers":[]}\n'
    '{"type":"result","is_error":true,'
    '"result":"Failed to authenticate. API Error: 401 OAuth access token has been revoked."}\n'
)


def _revoked_process(self, cmd, env):
    """Stand in for the claude subprocess: rc=1 plus the raw revoked-401 stream-json.
    Patched at ``_execute_process`` so ``_extract_raw``/``_parse_output`` still run."""
    return subprocess.CompletedProcess(cmd, 1, _REVOKED_RAW, "")


def test_revoked_token_throttles_and_fails_over_instead_of_dead_lettering(
    int_db_config, int_consumer_config,
):
    """Repro of the reported production failure.

    Pool mirrors the reported state: priority-0 token already throttled by a
    session limit from another job, priority-1 token whose stored access token is
    revoked, priority-2 token healthy and never tried. Before the fix the revoked
    401 degraded to a generic RuntimeError, so the token stayed ``status='ok'``,
    ``retry_with_other_token`` was never set, and the deterministic
    ``ORDER BY priority ASC`` re-picked the SAME revoked token on every attempt
    until the job dead-lettered. Expected now: the revoked token is THROTTLED (not
    poisoned), the job requeues, and attempt 2 lands on the priority-2 token and
    succeeds.
    """
    logger = logging.getLogger("test")
    _bind_provider("claude")
    token_limited = _seed_token("prio0-session-limited", priority=0)
    token_revoked = _seed_token("prio1-revoked", priority=1)
    token_healthy = _seed_token("prio2-healthy", priority=2)

    # Priority-0 is already in a session-limit cooldown from another job.
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE oauth_token SET throttled_until = UTC_TIMESTAMP() + INTERVAL 1 HOUR "
                "WHERE id = %s",
                (token_limited,),
            )
    finally:
        conn.close()

    job_id = insert_queued_job(
        reference_id="AI-REVOKED", idempotency_key="revoked-pool:1", max_attempts=3,
    )

    # Attempt 1: prio-0 skipped (throttled) -> prio-1 selected -> the real parser
    # classifies the raw 401 as transient -> throttle + requeue. No token_id on the
    # exception, so the consumer attributes it to the token IT resolved from the pool.
    with patch.object(TokenClaudeRunner, "_execute_process", new=_revoked_process):
        consumer = Consumer(int_db_config, int_consumer_config, logger)
        job = consumer._try_dequeue()
        assert job is not None
        consumer._execute_job(job)

    row = fetch_job(job_id)
    assert row["status"] == "TODO", "must requeue, not dead-letter"
    assert row["attempt"] == 1
    assert row["error_class"] == "TransientAuthError"
    # THE bug: the revoked token must be throttled, never poisoned.
    assert _token_status(token_revoked) == "ok"
    assert _token_throttle(token_revoked) is not None
    assert _token_throttle(token_revoked) > datetime.now(UTC).replace(tzinfo=None)
    # The untried healthy token is untouched and still selectable.
    assert _token_status(token_healthy) == "ok"
    assert _token_throttle(token_healthy) is None

    update_job(job_id, scheduled_after="2000-01-01 00:00:00")

    # Attempt 2: prio-0 and prio-1 both throttled -> prio-2 selected -> SUCCESS.
    # Patched at `run` here, not at the subprocess seam: the success path exercises no
    # classification, so there is nothing for the real parser to prove. Only the failing
    # attempt above needs to go through `parse_claude_output`.
    success = RunResult(
        raw_output="ok", input_tokens=1500, output_tokens=800, cost_usd=0.01,
        num_turns=1, duration_ms=1000, subtype="success", agent_type="claude",
    )
    with patch.object(TokenClaudeRunner, "run", return_value=success):
        consumer2 = Consumer(int_db_config, int_consumer_config, logger)
        job2 = consumer2._try_dequeue()
        assert job2 is not None
        assert job2.id == job_id
        consumer2._execute_job(job2)

    row = fetch_job(job_id)
    assert row["status"] == "SUCCESS", "the healthy prio-2 token must have been used"
    assert row["attempt"] == 2
    # No token was permanently poisoned — all three recover on their own.
    assert _token_status(token_limited) == "ok"
    assert _token_status(token_revoked) == "ok"
    assert _token_status(token_healthy) == "ok"


def test_revoked_token_dead_letters_only_once_the_pool_is_exhausted(
    int_db_config, int_consumer_config,
):
    """Failover is not infinite: with a single token in the pool a revoked 401 leaves
    no healthy alternative, so ``retry_with_other_token`` stays False and the job
    dead-letters immediately (the ``NON_RETRYABLE_ERRORS`` path) rather than burning
    all three attempts on the same credential."""
    logger = logging.getLogger("test")
    _bind_provider("claude")
    only_token = _seed_token("only-revoked", priority=0)
    job_id = insert_queued_job(
        reference_id="AI-REVOKED-SOLO", idempotency_key="revoked-pool:solo", max_attempts=3,
    )

    with patch.object(TokenClaudeRunner, "_execute_process", new=_revoked_process):
        consumer = Consumer(int_db_config, int_consumer_config, logger)
        job = consumer._try_dequeue()
        assert job is not None
        consumer._execute_job(job)

    row = fetch_job(job_id)
    assert row["status"] == "DEAD"
    assert row["attempt"] == 1
    assert row["error_class"] == "TransientAuthError"
    # Still a cooldown, not a poison — an operator does not need `token:reset`.
    assert _token_status(only_token) == "ok"
    assert _token_throttle(only_token) is not None
