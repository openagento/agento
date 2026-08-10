"""Unit: who gets an exclusive refresh lease (CredentialResolver policy) and how each
harness reports a credential's remaining lifetime.

The policy is deliberately conservative — unknown lifetime means exclusive — so these tests
are mostly about NOT leasing the things that must stay shared.
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.agent_manager.credential_resolver import CredentialResolver
from agento.framework.agent_manager.models import CredentialRecord, CredentialStatus

_HORIZON = 2100


def _token(credentials: dict | None, *, type: str = "oauth") -> CredentialRecord:
    now = datetime(2026, 8, 4, 12, 0, 0)
    return CredentialRecord(
        id=1,
        scope="claude",
        type=type,
        label="prod-1",
        credentials=credentials,
        token_limit=0,
        enabled=True,
        status=CredentialStatus.OK,
        priority=0,
        error_msg=None,
        expires_at=None,
        used_at=None,
        created_at=now,
        updated_at=now,
    )


def _resolver() -> CredentialResolver:
    return CredentialResolver(MagicMock(), refresh_horizon_seconds=_HORIZON, lease_ttl_seconds=300)


def _with_ttl(ttl):
    """Patch the scope's owning harness so its workspace adapter reports ``ttl`` seconds."""
    adapter = MagicMock()
    adapter.credential_ttl_seconds.return_value = ttl
    return patch(
        "agento.framework.harness.registry.get_harness_for_scope",
        return_value=MagicMock(adapter=MagicMock(workspace_adapter=adapter)),
    )


class TestRefreshImminent:
    def test_a_credential_with_nothing_to_rotate_is_always_shareable(self):
        # An anthropic_api_key row cannot burn a single-use secret, so it must keep the
        # full 10-way fan-out even though it has no expiry at all.
        with _with_ttl(0):
            assert _resolver()._refresh_imminent(_token({"subscription_key": "sk"})) is False

    def test_a_past_expiry_alone_does_not_make_an_api_key_exclusive(self):
        with _with_ttl(-99999):
            assert _resolver()._refresh_imminent(
                _token({"subscription_key": "sk"}, type="anthropic_api_key")
            ) is False

    def test_a_credential_just_inside_the_horizon_is_exclusive(self):
        with _with_ttl(_HORIZON - 1):
            assert _resolver()._refresh_imminent(
                _token({"refresh_token": "R0"})
            ) is True

    def test_a_credential_exactly_at_the_horizon_is_exclusive(self):
        with _with_ttl(_HORIZON):
            assert _resolver()._refresh_imminent(_token({"refresh_token": "R0"})) is True

    def test_a_credential_beyond_the_horizon_stays_shared(self):
        with _with_ttl(_HORIZON + 1):
            assert _resolver()._refresh_imminent(_token({"refresh_token": "R0"})) is False

    def test_unknown_lifetime_is_conservative(self):
        with _with_ttl(None):
            assert _resolver()._refresh_imminent(_token({"refresh_token": "R0"})) is True

    def test_an_unregistered_scope_is_conservative(self):
        # No owning harness (module disabled / never registered) -> unknown lifetime.
        with patch(
            "agento.framework.harness.registry.get_harness_for_scope", return_value=None
        ):
            assert _resolver()._refresh_imminent(_token({"refresh_token": "R0"})) is True

    def test_an_adapter_without_the_method_is_conservative(self):
        # The method is declared on the WorkspaceAdapter protocol, but a third-party
        # adapter that predates it must degrade conservatively, not crash a job.
        with patch(
            "agento.framework.harness.registry.get_harness_for_scope",
            return_value=MagicMock(
                adapter=MagicMock(workspace_adapter=MagicMock(spec=["write_credentials"]))
            ),
        ):
            assert _resolver()._refresh_imminent(_token({"refresh_token": "R0"})) is True

    def test_a_raising_hook_is_conservative(self):
        adapter = MagicMock()
        adapter.credential_ttl_seconds.side_effect = ValueError("boom")
        with patch(
            "agento.framework.harness.registry.get_harness_for_scope",
            return_value=MagicMock(adapter=MagicMock(workspace_adapter=adapter)),
        ):
            assert _resolver()._refresh_imminent(_token({"refresh_token": "R0"})) is True

    def test_a_raising_registry_lookup_is_conservative(self):
        with patch(
            "agento.framework.harness.registry.get_harness_for_scope",
            side_effect=KeyError("claude"),
        ):
            assert _resolver()._refresh_imminent(_token({"refresh_token": "R0"})) is True


class TestResolveLeaseWiring:
    def test_no_lease_owner_means_no_lease_requested(self):
        with patch(
            "agento.framework.agent_manager.credential_resolver.select_credential"
        ) as select:
            select.return_value = _token({"refresh_token": "R0"})
            _resolver().resolve(MagicMock(), "claude")
        assert select.call_args.args[2] is None

    def test_a_lease_owner_is_passed_through_with_the_policy(self):
        with patch(
            "agento.framework.agent_manager.credential_resolver.select_credential"
        ) as select:
            select.return_value = _token({"refresh_token": "R0"})
            resolver = _resolver()
            resolver.resolve(MagicMock(), "claude", lease_owner="job-1-attempt-1")
        lease = select.call_args.args[2]
        assert lease.owner == "job-1-attempt-1"
        assert lease.ttl_seconds == 300
        assert lease.should_lease == resolver._refresh_imminent

    def test_an_all_leased_pool_names_the_lease_in_its_error(self):
        with (
            patch(
                "agento.framework.agent_manager.credential_resolver.select_credential",
                return_value=None,
            ),
            patch(
                "agento.framework.agent_manager.credential_resolver.count_credentials_for_scope",
                return_value=(1, 1),
            ),
            pytest.raises(RuntimeError, match="currently locked"),
        ):
            _resolver().resolve(MagicMock(), "claude", lease_owner="job-1-attempt-1")

    def test_contention_retries_end_their_transaction_before_sleeping(self):
        # get_connection is autocommit=False at REPEATABLE READ, so without this the
        # snapshot would be replayed and the holder's commit never observed.
        conn = MagicMock()
        with (
            patch(
                "agento.framework.agent_manager.credential_resolver.select_credential",
                side_effect=[None, _token({"refresh_token": "R0"})],
            ),
            patch(
                "agento.framework.agent_manager.credential_resolver.count_credentials_for_scope",
                return_value=(2, 1),
            ),
        ):
            _resolver().resolve(conn, "claude")
        conn.commit.assert_called()


class TestClaudeCredentialTtl:
    def _writer(self):
        from agento.modules.claude.src.config import ClaudeWorkspaceAdapter
        return ClaudeWorkspaceAdapter()

    def _token_with_expires_at(self, expires_at):
        return _token({
            "refresh_token": "R0",
            "raw_auth": {"credentials": {"claudeAiOauth": {"expiresAt": expires_at}}},
        })

    def test_reads_epoch_milliseconds(self):
        soon = (datetime.now(UTC) + timedelta(seconds=600)).timestamp() * 1000
        ttl = self._writer().credential_ttl_seconds(self._token_with_expires_at(soon))
        assert 540 <= ttl <= 600

    def test_reads_legacy_epoch_seconds(self):
        soon = (datetime.now(UTC) + timedelta(seconds=600)).timestamp()
        ttl = self._writer().credential_ttl_seconds(self._token_with_expires_at(soon))
        assert 540 <= ttl <= 600

    def test_an_already_expired_credential_reports_a_negative_ttl(self):
        past = (datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000
        # Expired is "very imminent", NOT unknown — the caller's comparison handles it.
        assert self._writer().credential_ttl_seconds(self._token_with_expires_at(past)) < 0

    def test_api_key_rows_report_nothing(self):
        credential = _token({"subscription_key": "sk"}, type="anthropic_api_key")
        assert self._writer().credential_ttl_seconds(credential) is None

    @pytest.mark.parametrize("garbage", ["not-a-number", None, {}, True, 12345])
    def test_garbage_reports_nothing(self, garbage):
        # 12345 included: an implausible pre-2024 epoch is garbage, not "expired in 1970".
        assert self._writer().credential_ttl_seconds(
            self._token_with_expires_at(garbage)
        ) is None

    def test_a_missing_raw_auth_reports_nothing(self):
        assert self._writer().credential_ttl_seconds(_token({"refresh_token": "R0"})) is None


class TestCodexCredentialTtl:
    def _writer(self):
        from agento.modules.codex.src.config import CodexWorkspaceAdapter
        return CodexWorkspaceAdapter()

    def _jwt(self, claims: dict) -> str:
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"header.{payload}.signature"

    def _token_with(self, tokens: dict) -> CredentialRecord:
        return _token({"refresh_token": "R0", "raw_auth": {"tokens": tokens}})

    def test_reads_the_jwt_exp_claim(self):
        exp = int((datetime.now(UTC) + timedelta(seconds=900)).timestamp())
        ttl = self._writer().credential_ttl_seconds(
            self._token_with({"access_token": self._jwt({"exp": exp})})
        )
        assert 840 <= ttl <= 900

    def test_falls_back_to_the_iso_expiry_shape(self):
        expiry = (datetime.now(UTC) + timedelta(seconds=900)).isoformat().replace("+00:00", "Z")
        ttl = self._writer().credential_ttl_seconds(
            self._token_with({"access_token": "opaque-not-a-jwt", "expiry": expiry})
        )
        assert 840 <= ttl <= 900

    def test_a_naive_iso_expiry_is_read_as_utc(self):
        expiry = (datetime.now(UTC) + timedelta(seconds=900)).replace(tzinfo=None).isoformat()
        ttl = self._writer().credential_ttl_seconds(self._token_with({"expiry": expiry}))
        assert 840 <= ttl <= 900

    def test_an_opaque_access_token_with_no_expiry_reports_nothing(self):
        assert self._writer().credential_ttl_seconds(
            self._token_with({"access_token": "opaque"})
        ) is None

    def test_a_jwt_with_no_exp_claim_reports_nothing(self):
        assert self._writer().credential_ttl_seconds(
            self._token_with({"access_token": self._jwt({"sub": "x"})})
        ) is None

    def test_an_implausible_exp_reports_nothing(self):
        assert self._writer().credential_ttl_seconds(
            self._token_with({"access_token": self._jwt({"exp": 12345})})
        ) is None

    def test_a_malformed_payload_reports_nothing(self):
        assert self._writer().credential_ttl_seconds(
            self._token_with({"access_token": "header.!!!not-base64!!!.sig"})
        ) is None

    def test_api_key_rows_report_nothing(self):
        credential = _token({"subscription_key": "sk"}, type="codex_api_key")
        assert self._writer().credential_ttl_seconds(credential) is None
