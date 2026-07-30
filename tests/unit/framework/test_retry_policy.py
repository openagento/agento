from __future__ import annotations

from agento.framework.agent_manager.errors import AuthenticationError, UsageLimitError
from agento.framework.events import JobVerificationFailed, Verdict, VerifyReason
from agento.framework.retry_policy import BACKOFF_DELAYS, evaluate


def _auth_error(*, healthy_alternative: bool) -> AuthenticationError:
    """An AuthenticationError as the consumer hands it to evaluate(): the
    ``retry_with_other_token`` flag reflects whether the pool still has a
    healthy token to try after poisoning the offending one."""
    exc = AuthenticationError("401 Unauthorized", token_id=7)
    exc.retry_with_other_token = healthy_alternative
    return exc


def _veto(retryable: bool, reason: VerifyReason = VerifyReason.NO_MCP_CALLS) -> JobVerificationFailed:
    return JobVerificationFailed(Verdict(
        retryable=retryable,
        reason=reason,
        fresh_start=retryable,
    ))


def test_retryable_error_attempt_1():
    decision = evaluate("RuntimeError", attempt=1, max_attempts=3)
    assert decision.should_retry is True
    assert decision.delay_seconds == 60


def test_retryable_error_attempt_2():
    decision = evaluate("RuntimeError", attempt=2, max_attempts=3)
    assert decision.should_retry is True
    assert decision.delay_seconds == 300


def test_retryable_error_attempt_3_max_reached():
    decision = evaluate("RuntimeError", attempt=3, max_attempts=3)
    assert decision.should_retry is False


def test_non_retryable_value_error():
    decision = evaluate("ValueError", attempt=1, max_attempts=3)
    assert decision.should_retry is False
    assert "Non-retryable" in decision.reason


def test_non_retryable_permission_error():
    decision = evaluate("PermissionError", attempt=1, max_attempts=3)
    assert decision.should_retry is False


def test_non_retryable_key_error():
    decision = evaluate("KeyError", attempt=1, max_attempts=3)
    assert decision.should_retry is False


def test_unknown_error_is_retryable():
    decision = evaluate("RuntimeError", attempt=1, max_attempts=3)
    assert decision.should_retry is True


def test_none_error_class_is_retryable():
    decision = evaluate(None, attempt=1, max_attempts=3)
    assert decision.should_retry is True
    assert decision.delay_seconds == 60


def test_backoff_caps_at_last_delay():
    decision = evaluate("RuntimeError", attempt=10, max_attempts=20)
    assert decision.should_retry is True
    assert decision.delay_seconds == BACKOFF_DELAYS[-1]  # 1800


# -- JobVerificationFailed.verdict branch --------------------------------------
# These pin the contract added for the app_monitor verification gate: a
# verdict attached to the exception must drive the retry decision, overriding
# the default rules keyed on error_class.

def test_verification_veto_retryable_attempt_1():
    exc = _veto(retryable=True)
    decision = evaluate("JobVerificationFailed", attempt=1, max_attempts=3, error_obj=exc)
    assert decision.should_retry is True
    assert decision.delay_seconds == BACKOFF_DELAYS[0]
    assert "Verification veto retry" in decision.reason


def test_verification_veto_retryable_respects_max_attempts():
    exc = _veto(retryable=True)
    decision = evaluate("JobVerificationFailed", attempt=3, max_attempts=3, error_obj=exc)
    assert decision.should_retry is False
    assert "Max attempts" in decision.reason
    assert "verification veto" in decision.reason


def test_verification_veto_non_retryable_short_circuits():
    exc = _veto(retryable=False, reason=VerifyReason.TRANSCRIPT_MISSING)
    decision = evaluate("JobVerificationFailed", attempt=1, max_attempts=3, error_obj=exc)
    assert decision.should_retry is False
    assert decision.reason.startswith("Verification veto (non-retryable)")


def test_verification_veto_overrides_non_retryable_error_class():
    """A retryable verdict must override a normally-non-retryable error_class."""
    exc = _veto(retryable=True)
    decision = evaluate("ValueError", attempt=1, max_attempts=3, error_obj=exc)
    assert decision.should_retry is True
    assert decision.delay_seconds == BACKOFF_DELAYS[0]


def test_verification_veto_tightens_retryable_error_class():
    """A non-retryable verdict must override a normally-retryable error_class."""
    exc = _veto(retryable=False, reason=VerifyReason.TRANSCRIPT_PARSE_FAILED)
    decision = evaluate("RuntimeError", attempt=1, max_attempts=3, error_obj=exc)
    assert decision.should_retry is False
    assert decision.reason.startswith("Verification veto (non-retryable)")


def test_verification_veto_backoff_caps_at_last_delay():
    exc = _veto(retryable=True)
    decision = evaluate("JobVerificationFailed", attempt=10, max_attempts=20, error_obj=exc)
    assert decision.should_retry is True
    assert decision.delay_seconds == BACKOFF_DELAYS[-1]


# -- AuthenticationError pool-aware retry --------------------------------------
# AuthenticationError is normally terminal (a single bad credential won't fix
# itself on retry). But with an LRU token pool, a poisoned token leaves healthy
# alternatives — the consumer flags ``retry_with_other_token`` so the job
# retries onto the next token instead of dead-lettering immediately.

def test_auth_error_retries_when_healthy_alternative_exists():
    exc = _auth_error(healthy_alternative=True)
    decision = evaluate("AuthenticationError", attempt=1, max_attempts=3, error_obj=exc)
    assert decision.should_retry is True
    assert decision.delay_seconds == BACKOFF_DELAYS[0]


def test_auth_error_terminal_without_healthy_alternative():
    exc = _auth_error(healthy_alternative=False)
    decision = evaluate("AuthenticationError", attempt=1, max_attempts=3, error_obj=exc)
    assert decision.should_retry is False
    assert "Non-retryable" in decision.reason


def test_auth_error_with_alternative_respects_max_attempts():
    exc = _auth_error(healthy_alternative=True)
    decision = evaluate("AuthenticationError", attempt=3, max_attempts=3, error_obj=exc)
    assert decision.should_retry is False
    assert "Max attempts" in decision.reason


def test_auth_error_without_flag_is_terminal():
    """An auth error that never went through the pool (e.g. interactive login)
    carries no flag and stays non-retryable."""
    exc = AuthenticationError("Not logged in")
    decision = evaluate("AuthenticationError", attempt=1, max_attempts=3, error_obj=exc)
    assert decision.should_retry is False
    assert "Non-retryable" in decision.reason


def test_retry_flag_on_non_auth_error_does_not_bypass_non_retryable():
    """The pool-aware retry is scoped to AuthenticationError/UsageLimitError. A
    stray ``retry_with_other_token`` on an unrelated, normally-terminal error must
    NOT make it retryable."""
    exc = ValueError("boom")
    exc.retry_with_other_token = True  # type: ignore[attr-defined]
    decision = evaluate("ValueError", attempt=1, max_attempts=3, error_obj=exc)
    assert decision.should_retry is False
    assert "Non-retryable" in decision.reason


# -- UsageLimitError pool-aware failover ---------------------------------------
# A session/usage limit is TEMPORARY: the consumer throttles the offending token
# (cooldown, not poison) and flags ``retry_with_other_token`` when a healthy token
# remains, so the job fails over. Without a healthy alternative it is terminal for
# this job — but the token self-recovers after its cooldown. Mirrors auth behavior.

def _limit_error(*, healthy_alternative: bool) -> UsageLimitError:
    exc = UsageLimitError("hit your session limit", token_id=7)
    exc.retry_with_other_token = healthy_alternative
    return exc


def test_usage_limit_retries_when_healthy_alternative_exists():
    exc = _limit_error(healthy_alternative=True)
    decision = evaluate("UsageLimitError", attempt=1, max_attempts=3, error_obj=exc)
    assert decision.should_retry is True
    assert decision.delay_seconds == BACKOFF_DELAYS[0]
    assert "UsageLimitError retry" in decision.reason


def test_usage_limit_terminal_without_healthy_alternative():
    exc = _limit_error(healthy_alternative=False)
    decision = evaluate("UsageLimitError", attempt=1, max_attempts=3, error_obj=exc)
    assert decision.should_retry is False
    assert "Non-retryable" in decision.reason


def test_usage_limit_with_alternative_respects_max_attempts():
    exc = _limit_error(healthy_alternative=True)
    decision = evaluate("UsageLimitError", attempt=3, max_attempts=3, error_obj=exc)
    assert decision.should_retry is False
    assert "Max attempts" in decision.reason


# --- TransientAuthError ------------------------------------------------------
# A credential rejection that does NOT prove the token is dead (revoked/stale
# access token). Handled like a usage limit: cooldown + failover, never a poison.

def test_transient_auth_with_healthy_alternative_retries_onto_next_token():
    from agento.framework.agent_manager.errors import TransientAuthError

    exc = TransientAuthError("401 OAuth access token has been revoked")
    exc.retry_with_other_token = True
    decision = evaluate("TransientAuthError", attempt=1, max_attempts=3, error_obj=exc)

    assert decision.should_retry is True
    assert decision.delay_seconds == BACKOFF_DELAYS[0]
    assert "next healthy token" in decision.reason


def test_transient_auth_without_healthy_alternative_is_terminal():
    from agento.framework.agent_manager.errors import TransientAuthError

    exc = TransientAuthError("401 OAuth access token has been revoked")
    decision = evaluate("TransientAuthError", attempt=1, max_attempts=3, error_obj=exc)

    assert decision.should_retry is False
    assert "Non-retryable" in decision.reason


def test_transient_auth_max_attempts_reached_does_not_retry():
    from agento.framework.agent_manager.errors import TransientAuthError

    exc = TransientAuthError("401 OAuth access token has been revoked")
    exc.retry_with_other_token = True
    decision = evaluate("TransientAuthError", attempt=3, max_attempts=3, error_obj=exc)

    assert decision.should_retry is False
    assert "Max attempts" in decision.reason
