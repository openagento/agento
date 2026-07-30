from __future__ import annotations

from dataclasses import dataclass

# Backoff delays in seconds: 1 min, 5 min, 30 min
BACKOFF_DELAYS = [60, 300, 1800]

# Exception class names that should NOT be retried
NON_RETRYABLE_ERRORS = frozenset({
    "ValueError",
    "PermissionError",
    "FileNotFoundError",
    "KeyError",
    "AuthenticationError",  # token expired — retrying won't help
    "UsageLimitError",  # session/usage limit — terminal unless a healthy token remains
    "TransientAuthError",  # stale/revoked credential — terminal unless a healthy token remains
})


@dataclass
class RetryDecision:
    should_retry: bool
    delay_seconds: int
    reason: str


def evaluate(
    error_class: str | None,
    attempt: int,
    max_attempts: int,
    error_obj: Exception | None = None,
) -> RetryDecision:
    """Decide whether a failed job should be retried.

    Args:
        error_class: The __class__.__name__ of the caught exception.
        attempt: Current attempt number (1-indexed, just completed).
        max_attempts: Maximum allowed attempts.
        error_obj: The actual exception instance. When the exception carries a
            ``verdict`` attribute (``JobVerificationFailed``) the verdict's
            ``retryable`` flag overrides the standard rules — verification
            vetoes know more than the generic exception name.
    """
    verdict = getattr(error_obj, "verdict", None)
    if verdict is not None:
        if not verdict.retryable:
            return RetryDecision(
                should_retry=False,
                delay_seconds=0,
                reason=f"Verification veto (non-retryable): {verdict.reason}",
            )
        if attempt >= max_attempts:
            return RetryDecision(
                should_retry=False,
                delay_seconds=0,
                reason=f"Max attempts ({max_attempts}) reached after verification veto",
            )
        delay_index = min(attempt - 1, len(BACKOFF_DELAYS) - 1)
        delay = BACKOFF_DELAYS[delay_index]
        return RetryDecision(
            should_retry=True,
            delay_seconds=delay,
            reason=f"Verification veto retry {attempt + 1}/{max_attempts} after {delay}s",
        )

    # Auth failures and usage/session-limit hits are normally terminal — a single
    # bad or limited credential won't heal on the same token. But with an LRU token
    # pool, poisoning (auth) or throttling (usage-limit) the offending token leaves
    # healthy alternatives. The consumer sets ``retry_with_other_token`` on the
    # exception when another healthy token exists, so the job retries onto the next
    # token instead of dead-lettering on the first bad/limited credential.
    if error_class in (
        "AuthenticationError",
        "UsageLimitError",
        "TransientAuthError",
    ) and getattr(error_obj, "retry_with_other_token", False):
        if attempt >= max_attempts:
            return RetryDecision(
                should_retry=False,
                delay_seconds=0,
                reason=f"Max attempts ({max_attempts}) reached after {error_class}",
            )
        delay_index = min(attempt - 1, len(BACKOFF_DELAYS) - 1)
        delay = BACKOFF_DELAYS[delay_index]
        return RetryDecision(
            should_retry=True,
            delay_seconds=delay,
            reason=f"{error_class} retry {attempt + 1}/{max_attempts} after {delay}s (next healthy token)",
        )

    if error_class and error_class in NON_RETRYABLE_ERRORS:
        return RetryDecision(
            should_retry=False,
            delay_seconds=0,
            reason=f"Non-retryable error: {error_class}",
        )

    if attempt >= max_attempts:
        return RetryDecision(
            should_retry=False,
            delay_seconds=0,
            reason=f"Max attempts ({max_attempts}) reached",
        )

    delay_index = min(attempt - 1, len(BACKOFF_DELAYS) - 1)
    delay = BACKOFF_DELAYS[delay_index]

    return RetryDecision(
        should_retry=True,
        delay_seconds=delay,
        reason=f"Retry {attempt + 1}/{max_attempts} after {delay}s",
    )
