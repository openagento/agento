from __future__ import annotations


class AuthenticationError(RuntimeError):
    """Raised when agent authentication fails.

    Covers two related failure modes:
    * Interactive OAuth login (``credential:register`` / ``credential:refresh``) — raised
      before any credential record exists, so ``credential_id`` is ``None``.
    * Runtime auth rejection — the agent CLI rejected a stored credential while
      executing a job (401, expired token, "Not logged in"). The consumer sets
      ``credential_id`` to the id of the credential it handed to the runner so the
      pool can be updated (``status='error'``) and a different one selected on retry.
    """

    def __init__(self, message: str, *, credential_id: int | None = None) -> None:
        super().__init__(message)
        self.credential_id = credential_id
        # Set by the consumer after poisoning the offending credential: True when a
        # healthy alternative remains in the pool, so the job retries onto it
        # instead of dead-lettering. ``retry_policy.evaluate`` reads this flag.
        self.retry_with_other_token = False


class UsageLimitError(RuntimeError):
    """Raised when an agent CLI rejects a run because the account hit its
    session/usage/rate limit.

    Unlike ``AuthenticationError`` this is TEMPORARY, not a poisoned credential:
    the consumer throttles the offending token until ``reset_at`` (a cooldown via
    ``credential.throttled_until`` — NOT ``status='error'`` and NOT ``expires_at``)
    so the pool skips it while limited and auto-recovers afterwards, and the job
    fails over to another healthy credential. ``credential_id`` is set by the consumer to the
    token it handed to the runner. ``reset_at`` is a naive-UTC datetime supplied by
    the agent module's parser, or ``None`` when the CLI gave no parseable reset time
    (the consumer then applies a default throttle window).
    """

    def __init__(
        self,
        message: str,
        *,
        credential_id: int | None = None,
        reset_at=None,
    ) -> None:
        super().__init__(message)
        self.credential_id = credential_id
        self.reset_at = reset_at
        # Set by the consumer after throttling the offending credential: True when a
        # healthy alternative remains in the pool, so the job retries onto it.
        # ``retry_policy.evaluate`` reads this flag (mirrors AuthenticationError).
        self.retry_with_other_token = False


class TransientAuthError(RuntimeError):
    """Raised when an agent CLI rejects a stored credential in a way that does NOT
    prove the credential is dead — e.g. ``401 OAuth access token has been revoked``,
    usually a stale access-token copy from a concurrent refresh rather than a revoked
    account (the same token label keeps serving other jobs).

    Handled like ``UsageLimitError``, not ``AuthenticationError``: the consumer
    THROTTLES the token briefly (``credential.throttled_until``; ``status`` stays
    ``'ok'``) so the pool skips it, the job fails over, and the token auto-recovers
    with no operator action. ``retry_with_other_token`` is set by the consumer when a
    healthy alternative remains; ``retry_policy.evaluate`` reads it.

    Deliberately NOT a subclass of ``AuthenticationError``, whose ``except`` clause
    would otherwise poison it.
    """

    def __init__(self, message: str, *, credential_id: int | None = None) -> None:
        super().__init__(message)
        self.credential_id = credential_id
        self.retry_with_other_token = False


class CredentialLeasedError(RuntimeError):
    """Raised when a write would replace the payload of a credential that a running
    job currently holds a refresh lease on.

    A lease keeps the pool from *reselecting* the row; it does not by itself
    serialize credential *writes*. Without this guard an operator
    ``credential:refresh`` could replace the blob mid-job, and the leaseholder's own
    ``capture_refreshed_credentials`` — rotating from the chain it materialized
    before the refresh — would then overwrite the operator's brand-new credential
    with a descendant of the old one. So ``register_credential`` refuses instead, and
    the CLI tells the operator who holds the lease and until when. Refusing (not
    waiting) is deliberate: an interactive OAuth flow has already spent a browser
    round-trip by the time we get here.
    """

    def __init__(self, label: str, lease_owner: str | None, leased_until=None) -> None:
        super().__init__(
            f"Credential {label!r} is leased by {lease_owner or '?'} until {leased_until} "
            f"(UTC) — retry after that, or stop the job holding it."
        )
        self.label = label
        self.lease_owner = lease_owner
        self.leased_until = leased_until
