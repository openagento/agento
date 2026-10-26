from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agento.framework.agent_manager.credential_resolver import CredentialResolver

from .conftest import make_token


class TestCredentialResolver:
    def test_resolve_returns_whatever_select_credential_yields(self):
        expected = make_token(id=2)

        with patch(
            "agento.framework.agent_manager.credential_resolver.select_credential",
            return_value=expected,
        ) as mock_select:
            resolver = CredentialResolver()
            token = resolver.resolve(MagicMock(), "claude")

        assert token is expected
        mock_select.assert_called_once()
        assert mock_select.call_args[0][1] == "claude"

    def test_a_pinned_credential_that_is_not_usable_fails_at_once(self):
        """Healthy peers exist, but waiting on the pool cannot free the pinned row."""
        with (
            patch(
                "agento.framework.agent_manager.credential_resolver.select_credential",
                return_value=None,
            ) as mock_select,
            patch(
                "agento.framework.agent_manager.credential_resolver.count_credentials_for_scope",
                return_value=(3, 3),
            ),pytest.raises(RuntimeError, match="id=7 is not usable")
        ):
            CredentialResolver().resolve(MagicMock(), "claude", credential_id=7)

        mock_select.assert_called_once()
        assert mock_select.call_args.kwargs["credential_id"] == 7

    def test_resolve_raises_when_no_tokens_registered(self):
        with (
            patch(
                "agento.framework.agent_manager.credential_resolver.select_credential",
                return_value=None,
            ),
            patch(
                "agento.framework.agent_manager.credential_resolver.count_credentials_for_scope",
                return_value=(0, 0),
            ),
        ):
            resolver = CredentialResolver()
            with pytest.raises(RuntimeError, match="No enabled credentials"):
                resolver.resolve(MagicMock(), "claude")

    def test_resolve_raises_when_all_errored_or_expired(self):
        with (
            patch(
                "agento.framework.agent_manager.credential_resolver.select_credential",
                return_value=None,
            ),
            patch(
                "agento.framework.agent_manager.credential_resolver.count_credentials_for_scope",
                return_value=(3, 0),
            ),
        ):
            resolver = CredentialResolver()
            with pytest.raises(RuntimeError, match=r"3 enabled credentials.*unhealthy"):
                resolver.resolve(MagicMock(), "codex")

    def test_resolve_retries_when_healthy_tokens_are_locked(self):
        expected = make_token(id=2)

        with (
            patch(
                "agento.framework.agent_manager.credential_resolver.select_credential",
                side_effect=[None, None, expected],
            ) as mock_select,
            patch(
                "agento.framework.agent_manager.credential_resolver.count_credentials_for_scope",
                return_value=(3, 3),
            ),
            patch("agento.framework.agent_manager.credential_resolver.time.sleep") as mock_sleep,
        ):
            token = CredentialResolver().resolve(MagicMock(), "claude")

        assert token is expected
        assert mock_select.call_count == 3
        assert mock_sleep.call_count == 2

    def test_resolve_reports_locked_tokens_after_retry_budget(self):
        with (
            patch(
                "agento.framework.agent_manager.credential_resolver.select_credential",
                return_value=None,
            ),
            patch(
                "agento.framework.agent_manager.credential_resolver.count_credentials_for_scope",
                return_value=(3, 3),
            ),
            patch("agento.framework.agent_manager.credential_resolver.time.sleep"),
            patch(
                "agento.framework.agent_manager.credential_resolver."
                "_POOL_CONTENTION_BUDGET_SECONDS",
                0.0,
            ),
            pytest.raises(RuntimeError, match="currently locked"),
        ):
            CredentialResolver().resolve(MagicMock(), "claude")

    def test_busy_pool_carries_lease_expiry_as_pool_retry_at(self):
        """AG-46: when every healthy token is busy/leased, resolve raises
        CredentialsBusyError carrying the earliest lease expiry so the consumer waits for
        the pool to free up instead of dead-lettering the job."""
        from datetime import datetime

        from agento.framework.agent_manager.errors import CredentialsBusyError

        lease_expiry = datetime(2026, 8, 28, 12, 5, 0)
        with (
            patch(
                "agento.framework.agent_manager.credential_resolver.select_credential",
                return_value=None,
            ),
            patch(
                "agento.framework.agent_manager.credential_resolver.count_credentials_for_scope",
                return_value=(1, 1),
            ),
            patch(
                "agento.framework.agent_manager.credential_resolver.earliest_lease_expiry_for_scope",
                return_value=lease_expiry,
            ),
            patch("agento.framework.agent_manager.credential_resolver.time.sleep"),
            patch(
                "agento.framework.agent_manager.credential_resolver."
                "_POOL_CONTENTION_BUDGET_SECONDS",
                0.0,
            ),pytest.raises(CredentialsBusyError) as exc_info
        ):
            CredentialResolver().resolve(MagicMock(), "claude")

        exc = exc_info.value
        assert exc.pool_retry_at == lease_expiry
        # The whole pool is busy — there is no other token to fail over to.
        assert exc.retry_with_other_token is False

    def test_busy_pool_pool_retry_at_none_for_pure_row_lock(self):
        """Pure row-lock contention carries no ``leased_until``; ``pool_retry_at`` stays
        None so the consumer falls back to ordinary backoff rather than a lease wait."""
        from agento.framework.agent_manager.errors import CredentialsBusyError

        with (
            patch(
                "agento.framework.agent_manager.credential_resolver.select_credential",
                return_value=None,
            ),
            patch(
                "agento.framework.agent_manager.credential_resolver.count_credentials_for_scope",
                return_value=(2, 2),
            ),
            patch(
                "agento.framework.agent_manager.credential_resolver.earliest_lease_expiry_for_scope",
                return_value=None,
            ),
            patch("agento.framework.agent_manager.credential_resolver.time.sleep"),
            patch(
                "agento.framework.agent_manager.credential_resolver."
                "_POOL_CONTENTION_BUDGET_SECONDS",
                0.0,
            ),pytest.raises(CredentialsBusyError) as exc_info
        ):
            CredentialResolver().resolve(MagicMock(), "claude")

        assert exc_info.value.pool_retry_at is None

    def test_resolve_outlasts_contention_longer_than_a_fixed_attempt_cap(self):
        """Regression: the retry budget must be wall-clock, not a fixed count.

        A 20-attempt cap gave up after ~200ms, which a herd of ~10 workers
        contending over a few rows exceeds on a loaded machine — the pool was
        healthy, every row was merely locked in passing, and the job died.
        """
        expected = make_token(id=2)
        attempts_under_contention = 40

        with (
            patch(
                "agento.framework.agent_manager.credential_resolver.select_credential",
                side_effect=[None] * attempts_under_contention + [expected],
            ) as mock_select,
            patch(
                "agento.framework.agent_manager.credential_resolver.count_credentials_for_scope",
                return_value=(3, 3),
            ),
            patch("agento.framework.agent_manager.credential_resolver.time.sleep"),
        ):
            token = CredentialResolver().resolve(MagicMock(), "claude")

        assert token is expected
        assert mock_select.call_count == attempts_under_contention + 1

    def test_resolve_gives_up_at_the_wall_clock_deadline(self):
        """Unbounded contention must still terminate — via the deadline.

        Pinned to an explicit budget so the bound is derived from the deadline
        under test, not from whatever the shipped constant happens to be.
        """
        budget = 3.0
        tick = 0.5
        clock = iter([float(i) * tick for i in range(1000)])

        with (
            patch(
                "agento.framework.agent_manager.credential_resolver.select_credential",
                return_value=None,
            ) as mock_select,
            patch(
                "agento.framework.agent_manager.credential_resolver.count_credentials_for_scope",
                return_value=(3, 3),
            ),
            patch("agento.framework.agent_manager.credential_resolver.time.sleep"),
            patch(
                "agento.framework.agent_manager.credential_resolver."
                "_POOL_CONTENTION_BUDGET_SECONDS",
                budget,
            ),
            patch(
                "agento.framework.agent_manager.credential_resolver.time.monotonic",
                side_effect=lambda: next(clock),
            ),
            pytest.raises(RuntimeError, match="currently locked"),
        ):
            CredentialResolver().resolve(MagicMock(), "claude")

        # Two clock reads per loop (deadline check), so the budget is spent
        # after ~budget/tick reads — bounded regardless of the shipped default.
        assert mock_select.call_count <= int(budget / tick) + 1

    def test_contention_backoff_is_jittered_and_capped(self):
        """Lockstep retries re-collide; jitter is what breaks the herd up."""
        expected = make_token(id=2)
        sleeps: list[float] = []

        with (
            patch(
                "agento.framework.agent_manager.credential_resolver.select_credential",
                side_effect=[None] * 30 + [expected],
            ),
            patch(
                "agento.framework.agent_manager.credential_resolver.count_credentials_for_scope",
                return_value=(3, 3),
            ),
            patch(
                "agento.framework.agent_manager.credential_resolver.time.sleep",
                side_effect=sleeps.append,
            ),
        ):
            CredentialResolver().resolve(MagicMock(), "claude")

        assert len(sleeps) == 30
        # Jitter: identical delays every round would keep the herd in lockstep.
        assert len(set(sleeps)) > 1
        # Backoff grows but stays capped (x1.5 = the jitter ceiling).
        assert max(sleeps) <= 0.1 * 1.5
        assert sum(sleeps[-5:]) > sum(sleeps[:5])

    def test_resolve_error_mentions_recovery_commands(self):
        with (
            patch(
                "agento.framework.agent_manager.credential_resolver.select_credential",
                return_value=None,
            ),
            patch(
                "agento.framework.agent_manager.credential_resolver.count_credentials_for_scope",
                return_value=(2, 0),
            ),
        ):
            resolver = CredentialResolver()
            with pytest.raises(RuntimeError) as exc_info:
                resolver.resolve(MagicMock(), "claude")

        msg = str(exc_info.value)
        assert "credential:refresh" in msg
        assert "credential:reset" in msg

    def test_resolve_passes_provider_through(self):
        with patch(
            "agento.framework.agent_manager.credential_resolver.select_credential",
            return_value=make_token(id=1, agent_type="codex"),
        ) as mock_select:
            resolver = CredentialResolver()
            resolver.resolve(MagicMock(), "codex")

        assert mock_select.call_args[0][1] == "codex"


@pytest.mark.parametrize("counts", [(0, 0), (3, 0)])
def test_an_empty_or_unhealthy_pool_is_an_auth_wait(counts):
    """WI-6: no usable credential is a wait for the operator, not a failure. Both
    sites raise the one marker class with the one retry time."""
    from datetime import UTC, datetime

    from agento.framework.agent_manager.errors import (
        AUTH_WAIT_RETRY,
        CredentialsUnavailableError,
    )

    with (
        patch(
            "agento.framework.agent_manager.credential_resolver.select_credential",
            return_value=None,
        ),
        patch(
            "agento.framework.agent_manager.credential_resolver.count_credentials_for_scope",
            return_value=counts,
        ),
    ):
        before = datetime.now(UTC).replace(tzinfo=None)
        with pytest.raises(CredentialsUnavailableError) as exc_info:
            CredentialResolver().resolve(MagicMock(), "claude")

    retry_at = exc_info.value.pool_retry_at
    assert retry_at.tzinfo is None
    assert before + AUTH_WAIT_RETRY <= retry_at <= datetime.now(UTC).replace(tzinfo=None) + AUTH_WAIT_RETRY
