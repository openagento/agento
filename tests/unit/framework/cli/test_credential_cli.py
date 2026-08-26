"""Tests for the credential:* CLI commands — specifically that ``credential:mark-error``
dispatches ``credential_auth_failed_after`` (and its legacy ``token_auth_failed_after``
alias) so workspace builds get re-materialized with the next healthy credential."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.agent_manager.models import CredentialRecord, CredentialStatus
from agento.framework.cli.credential import CredentialMarkErrorCommand
from agento.framework.harness import CredentialRegistrationMode

pytestmark = pytest.mark.usefixtures("builtin_harnesses")


def _make_args(credential_id: int = 6, message: str = "refresh-failed"):
    return argparse.Namespace(credential_id=credential_id, message=message)


def _make_token(scope: str = "codex", credential_id: int = 6) -> CredentialRecord:
    now = datetime.now(UTC).replace(tzinfo=None)
    return CredentialRecord(
        id=credential_id,
        scope=scope,
        type="oauth",
        label="client-it@example.com",
        credentials={"subscription_key": "sk-broken"},
        token_limit=0,
        enabled=True,
        status=CredentialStatus.ERROR,
        priority=0,
        error_msg=None,
        expires_at=None,
        used_at=None,
        created_at=now,
        updated_at=now,
    )


class TestCredentialMarkErrorCommand:
    @patch("agento.framework.cli.credential.get_connection_or_exit")
    @patch("agento.framework.cli.credential._load_framework_config")
    @patch("agento.framework.agent_manager.credential_store.get_credential")
    @patch("agento.framework.agent_manager.mark_credential_error")
    @patch("agento.framework.event_manager.get_event_manager")
    def test_dispatches_credential_auth_failed_after_on_success(
        self,
        mock_get_events,
        mock_mark,
        mock_get_credential,
        mock_config,
        mock_conn_fn,
    ):
        mock_config.return_value = ({}, None, None)
        mock_conn_fn.return_value = MagicMock()
        mock_get_credential.return_value = _make_token()
        mock_mark.return_value = True
        events = MagicMock()
        mock_get_events.return_value = events

        CredentialMarkErrorCommand().execute(_make_args())

        # Dual dispatch: the new name plus the deprecated alias, both built from
        # the same data so an observer on either name sees consistent values.
        dispatched = {name: payload for name, payload in
                      (c.args for c in events.dispatch.call_args_list)}
        assert set(dispatched) == {"credential_auth_failed_after", "token_auth_failed_after"}

        new = dispatched["credential_auth_failed_after"]
        assert (new.scope, new.credential_id) == ("codex", 6)
        assert new.error_msg == "refresh-failed"
        assert new.job_id is None

        legacy = dispatched["token_auth_failed_after"]
        assert (legacy.agent_type, legacy.token_id) == ("codex", 6)
        assert legacy.error_msg == "refresh-failed"

    @patch("agento.framework.cli.credential.get_connection_or_exit")
    @patch("agento.framework.cli.credential._load_framework_config")
    @patch("agento.framework.agent_manager.credential_store.get_credential")
    @patch("agento.framework.agent_manager.mark_credential_error")
    @patch("agento.framework.event_manager.get_event_manager")
    def test_does_not_dispatch_when_token_missing(
        self,
        mock_get_events,
        mock_mark,
        mock_get_credential,
        mock_config,
        mock_conn_fn,
    ):
        mock_config.return_value = ({}, None, None)
        mock_conn_fn.return_value = MagicMock()
        mock_get_credential.return_value = None
        mock_mark.return_value = False
        events = MagicMock()
        mock_get_events.return_value = events

        with pytest.raises(SystemExit):
            CredentialMarkErrorCommand().execute(_make_args())

        events.dispatch.assert_not_called()


def _stdin_with(text: str, *, isatty: bool = False):
    """Build a stdin substitute exposing readline() + isatty()."""
    import io
    fake = io.StringIO(text)
    fake.isatty = lambda: isatty  # type: ignore[assignment]
    return fake


class TestWithAccessToken:
    def test_codex_dispatches_to_register_from_access_token(self, monkeypatch):
        from agento.framework.cli.credential import _resolve_credentials

        args = argparse.Namespace(
            scope="codex", label="my-at",
            with_api_key=False, with_access_token=True,
            token_limit=0,
        )
        monkeypatch.setattr("sys.stdin", _stdin_with("eyJ.payload.sig\n"))
        strategy = MagicMock()
        strategy.register_from_secret.return_value = (
            {"access_token": "eyJ.payload.sig", "expires_at": 9999999999},
            "codex_access_token",
        )
        with patch("agento.framework.harness.get_authenticator", return_value=strategy):
            creds, type_ = _resolve_credentials(args, "codex", MagicMock())

        mode, secret = strategy.register_from_secret.call_args.args
        assert mode is CredentialRegistrationMode.ACCESS_TOKEN
        assert secret == "eyJ.payload.sig"
        assert type_ == "codex_access_token"
        assert creds["access_token"] == "eyJ.payload.sig"


class TestWithApiKey:
    def test_codex_dispatches_with_openai_api_key_type(self, monkeypatch):
        from agento.framework.cli.credential import _resolve_credentials

        args = argparse.Namespace(
            scope="codex", label="my-ak",
            with_api_key=True, with_access_token=False,
            token_limit=0,
        )
        monkeypatch.setattr("sys.stdin", _stdin_with("sk-X\n"))
        strategy = MagicMock()
        strategy.register_from_secret.return_value = ({"api_key": "sk-X"}, "openai_api_key")
        with patch("agento.framework.harness.get_authenticator", return_value=strategy):
            _creds, type_ = _resolve_credentials(args, "codex", MagicMock())

        mode, secret = strategy.register_from_secret.call_args.args
        assert mode is CredentialRegistrationMode.API_KEY
        assert secret == "sk-X"
        assert type_ == "openai_api_key"

    def test_claude_dispatches_with_anthropic_api_key_type(self, monkeypatch):
        from agento.framework.cli.credential import _resolve_credentials

        args = argparse.Namespace(
            scope="claude", label="my-ak",
            with_api_key=True, with_access_token=False,
            token_limit=0,
        )
        monkeypatch.setattr("sys.stdin", _stdin_with("sk-ant-X\n"))
        strategy = MagicMock()
        strategy.register_from_secret.return_value = ({"api_key": "sk-ant-X"}, "anthropic_api_key")
        with patch("agento.framework.harness.get_authenticator", return_value=strategy):
            _creds, type_ = _resolve_credentials(args, "claude", MagicMock())
        assert type_ == "anthropic_api_key"


class TestMutualExclusion:
    def test_argparse_rejects_both_flags(self):
        from agento.framework.cli.credential import CredentialRegisterCommand
        parser = argparse.ArgumentParser()
        CredentialRegisterCommand().configure(parser)
        with pytest.raises(SystemExit):
            parser.parse_args(["codex", "lbl", "--with-api-key", "--with-access-token"])


class TestMask:
    def test_short_secret_fully_masked(self):
        from agento.framework.cli.credential import _mask
        assert _mask("abc") == "***"
        assert _mask("12345678") == "********"

    def test_long_secret_shows_first4_last4(self):
        from agento.framework.cli.credential import _mask
        assert _mask("sk-proj-abc123XYZ") == "sk-p*********3XYZ"


class TestReadSecretStdin:
    def test_reads_one_line_from_non_tty_stdin(self, monkeypatch):
        from agento.framework.cli.credential import _read_secret
        monkeypatch.setattr("sys.stdin", _stdin_with("sk-X\n"))
        assert _read_secret("p:") == "sk-X"

    def test_uses_getpass_on_tty(self, monkeypatch):
        from agento.framework.cli import credential as credential_cli
        monkeypatch.setattr("sys.stdin", _stdin_with("", isatty=True))
        monkeypatch.setattr(credential_cli.getpass, "getpass", lambda _p: "tty-secret")
        assert credential_cli._read_secret("p:") == "tty-secret"

    def test_empty_stdin_exits(self, monkeypatch):
        from agento.framework.cli.credential import _read_secret
        monkeypatch.setattr("sys.stdin", _stdin_with("\n"))
        with pytest.raises(SystemExit):
            _read_secret("p:")


class TestArgparseRejectsInlineValue:
    def test_with_api_key_rejects_inline_value(self):
        from agento.framework.cli.credential import CredentialRegisterCommand
        parser = argparse.ArgumentParser()
        CredentialRegisterCommand().configure(parser)
        with pytest.raises(SystemExit):
            parser.parse_args(["codex", "lbl", "--with-api-key", "sk-XXX"])

    def test_with_api_key_no_value_is_accepted(self):
        from agento.framework.cli.credential import CredentialRegisterCommand
        parser = argparse.ArgumentParser()
        CredentialRegisterCommand().configure(parser)
        ns = parser.parse_args(["codex", "lbl", "--with-api-key"])
        assert ns.with_api_key is True
        assert ns.with_access_token is False


class TestMaskedEchoToStderr:
    def test_full_secret_never_appears_in_stderr(self, monkeypatch, capsys):
        from agento.framework.cli.credential import _resolve_credentials

        args = argparse.Namespace(
            scope="codex", label="lbl",
            with_api_key=True, with_access_token=False,
            token_limit=0,
        )
        monkeypatch.setattr("sys.stdin", _stdin_with("sk-piped-XYZ\n"))
        strategy = MagicMock()
        strategy.register_from_secret.return_value = (
            {"api_key": "sk-piped-XYZ"}, "openai_api_key")
        with patch("agento.framework.harness.get_authenticator", return_value=strategy):
            _resolve_credentials(args, "codex", MagicMock())

        err = capsys.readouterr().err
        assert "sk-p" in err and "-XYZ" in err
        assert "sk-piped-XYZ" not in err


class TestPositionalRemoved:
    def test_positional_credentials_path_no_longer_accepted(self):
        from agento.framework.cli.credential import CredentialRegisterCommand
        parser = argparse.ArgumentParser()
        CredentialRegisterCommand().configure(parser)
        with pytest.raises(SystemExit):
            parser.parse_args(["codex", "lbl", "/path/to/creds.json"])


class TestSetPriorityCommand:
    def test_set_priority_dispatches_to_store(self, capsys):
        from agento.framework.cli.credential import CredentialSetPriorityCommand
        args = argparse.Namespace(credential_id=42, priority=5)
        with patch("agento.framework.cli.credential._load_framework_config",
                   return_value=(MagicMock(), MagicMock(), MagicMock())), \
             patch("agento.framework.cli.credential.get_connection_or_exit",
                   return_value=MagicMock()), \
             patch("agento.framework.agent_manager.credential_store.set_credential_priority",
                   return_value=True) as mock_set:
            CredentialSetPriorityCommand().execute(args)
        mock_set.assert_called_once()
        # set_credential_priority(conn, credential_id, priority, logger=...)
        call_args = mock_set.call_args
        assert call_args.args[1] == 42 and call_args.args[2] == 5

    def test_set_priority_errors_when_token_missing(self, capsys):
        from agento.framework.cli.credential import CredentialSetPriorityCommand
        args = argparse.Namespace(credential_id=999, priority=1)
        with patch("agento.framework.cli.credential._load_framework_config",
                   return_value=(MagicMock(), MagicMock(), MagicMock())), \
             patch("agento.framework.cli.credential.get_connection_or_exit",
                   return_value=MagicMock()), \
             patch("agento.framework.agent_manager.credential_store.set_credential_priority",
                   return_value=False), \
             pytest.raises(SystemExit):
            CredentialSetPriorityCommand().execute(args)

    def test_set_priority_configure_accepts_int_args(self):
        from agento.framework.cli.credential import CredentialSetPriorityCommand
        parser = argparse.ArgumentParser()
        CredentialSetPriorityCommand().configure(parser)
        ns = parser.parse_args(["42", "5"])
        assert ns.credential_id == 42 and ns.priority == 5


class TestCredentialListShowsTypeAndPriority:
    def _token(self, id_, type_, priority):
        from datetime import datetime

        from agento.framework.agent_manager.models import (
            CredentialRecord,
            CredentialStatus,
        )
        return CredentialRecord(
            id=id_, scope="codex", type=type_, label=f"t{id_}",
            credentials=None, token_limit=0, enabled=True,
            status=CredentialStatus.OK, priority=priority, error_msg=None,
            expires_at=None, used_at=None,
            created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
        )

    def test_json_output_includes_type_and_priority(self, capsys):
        import json

        from agento.framework.cli.credential import CredentialListCommand

        tokens = [self._token(1, "codex_access_token", 5), self._token(2, "oauth", 0)]
        args = argparse.Namespace(scope=None, all=False, json=True)
        with patch("agento.framework.cli.credential._load_framework_config",
                   return_value=(MagicMock(), MagicMock(), MagicMock(usage_window_hours=24))), \
             patch("agento.framework.cli.credential.get_connection_or_exit"), \
             patch("agento.framework.agent_manager.list_credentials", return_value=tokens), \
             patch("agento.framework.agent_manager.get_usage_summaries", return_value=[]):
            CredentialListCommand().execute(args)

        out = json.loads(capsys.readouterr().out)
        by_id = {r["id"]: r for r in out}
        assert by_id[1]["type"] == "codex_access_token"
        assert by_id[1]["priority"] == 5
        assert by_id[2]["type"] == "oauth"
        assert by_id[2]["priority"] == 0

    def test_json_output_serializes_decimal_usage(self, capsys):
        """Regression: MySQL SUM() returns usage totals as Decimal, which
        json.dumps cannot serialize. ``token:list --json`` must coerce them."""
        import json
        from decimal import Decimal
        from types import SimpleNamespace

        from agento.framework.cli.credential import CredentialListCommand

        tokens = [self._token(1, "anthropic_api_key", 0)]
        summary = SimpleNamespace(credential_id=1, total_tokens=Decimal("12345"), call_count=7)
        args = argparse.Namespace(scope=None, all=False, json=True)
        with patch("agento.framework.cli.credential._load_framework_config",
                   return_value=(MagicMock(), MagicMock(), MagicMock(usage_window_hours=24))), \
             patch("agento.framework.cli.credential.get_connection_or_exit"), \
             patch("agento.framework.agent_manager.list_credentials", return_value=tokens), \
             patch("agento.framework.agent_manager.get_usage_summaries", return_value=[summary]):
            CredentialListCommand().execute(args)

        out = json.loads(capsys.readouterr().out)
        assert out[0]["tokens_used"] == 12345
        assert isinstance(out[0]["tokens_used"], int)

    def test_text_output_includes_type_and_priority(self, capsys):
        from agento.framework.cli.credential import CredentialListCommand

        tokens = [self._token(1, "codex_access_token", 5)]
        args = argparse.Namespace(scope=None, all=False, json=False)
        with patch("agento.framework.cli.credential._load_framework_config",
                   return_value=(MagicMock(), MagicMock(), MagicMock(usage_window_hours=24))), \
             patch("agento.framework.cli.credential.get_connection_or_exit"), \
             patch("agento.framework.agent_manager.list_credentials", return_value=tokens), \
             patch("agento.framework.agent_manager.get_usage_summaries", return_value=[]):
            CredentialListCommand().execute(args)
        out = capsys.readouterr().out
        assert "type=codex_access_token" in out
        assert "priority=5" in out


class TestCredentialListSurfacesProvenanceAndLease:
    """These three fields are what the rollout/verification steps grep for, so they are
    operator-facing output, not internals."""

    def _token(self, **overrides):
        from datetime import UTC, datetime, timedelta

        fields = dict(
            id=1, scope="claude", type="oauth", label="prod-1",
            credentials=None, token_limit=0, enabled=True, status=CredentialStatus.OK,
            priority=0, error_msg=None, expires_at=None, used_at=None,
            created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
        )
        fields.update(overrides)
        if fields.get("leased_until") == "future":
            fields["leased_until"] = (
                datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
            )
        return CredentialRecord(**fields)

    def _run(self, credentials, *, as_json: bool, capsys):
        from agento.framework.cli.credential import CredentialListCommand

        args = argparse.Namespace(scope=None, all=True, json=as_json)
        with patch("agento.framework.cli.credential._load_framework_config",
                   return_value=(MagicMock(), MagicMock(), MagicMock(usage_window_hours=24))), \
             patch("agento.framework.cli.credential.get_connection_or_exit"), \
             patch("agento.framework.agent_manager.list_credentials", return_value=credentials), \
             patch("agento.framework.agent_manager.get_usage_summaries", return_value=[]):
            CredentialListCommand().execute(args)
        return capsys.readouterr().out

    def test_json_exposes_error_source_and_lease_fields(self, capsys):
        import json

        credential = self._token(
            status=CredentialStatus.ERROR, error_msg="401", error_source="auto",
            lease_owner="job-118-attempt-1", leased_until="future",
        )
        row = json.loads(self._run([credential], as_json=True, capsys=capsys))[0]
        assert row["error_source"] == "auto"
        assert row["lease_owner"] == "job-118-attempt-1"
        assert row["leased_until"] is not None

    def test_text_annotates_provenance_and_a_live_lease(self, capsys):
        credential = self._token(
            status=CredentialStatus.ERROR, error_msg="401", error_source="auto",
            lease_owner="job-118-attempt-1", leased_until="future",
        )
        out = self._run([credential], as_json=False, capsys=capsys)
        assert "status=error (auto)" in out
        assert "refresh lease held by job-118-attempt-1" in out

    def test_a_pre_migration_quarantine_is_shown_as_unknown_provenance(self, capsys):
        # error_source IS NULL (production id=1 after 034): unknown, therefore operator —
        # it will NOT self-clear, and the operator needs to see that.
        credential = self._token(status=CredentialStatus.ERROR, error_msg="401", error_source=None)
        assert "status=error (operator?)" in self._run([credential], as_json=False, capsys=capsys)

    def test_an_expired_lease_is_not_announced_as_held(self, capsys):
        from datetime import UTC, datetime, timedelta

        token = self._token(
            lease_owner="job-1-attempt-1",
            leased_until=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5),
        )
        assert "refresh lease held by" not in self._run([token], as_json=False, capsys=capsys)


class TestCredentialRegisterEventCarriesType:
    """credential:register dispatches CredentialRegisteredEvent with the right type."""

    def _make_registered_token(self, type_: str) -> CredentialRecord:
        now = datetime.now(UTC).replace(tzinfo=None)
        return CredentialRecord(
            id=7,
            scope="codex",
            type=type_,
            label="my-label",
            credentials={"api_key": "sk-X"},
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

    @patch("agento.framework.cli.credential.get_connection_or_exit")
    @patch("agento.framework.cli.credential._load_framework_config")
    @patch("agento.framework.cli.credential._resolve_credentials")
    @patch("agento.framework.agent_manager.register_credential")
    @patch("agento.framework.event_manager.get_event_manager")
    def test_dispatched_event_carries_openai_api_key_type(
        self,
        mock_get_events,
        mock_register,
        mock_resolve,
        mock_config,
        mock_conn_fn,
    ):
        from agento.framework.cli.credential import CredentialRegisterCommand

        mock_config.return_value = ({}, None, None)
        conn = MagicMock()
        mock_conn_fn.return_value = conn
        mock_resolve.return_value = ({"api_key": "sk-X"}, "openai_api_key")
        mock_register.return_value = self._make_registered_token("openai_api_key")
        events = MagicMock()
        mock_get_events.return_value = events

        args = argparse.Namespace(
            scope="codex",
            label="my-label",
            with_api_key="sk-X",
            with_access_token=None,
            token_limit=0,
        )
        CredentialRegisterCommand().execute(args)

        dispatched = {name: payload for name, payload in
                      (c.args for c in events.dispatch.call_args_list)}
        assert set(dispatched) == {"credential_register_after", "token_register_after"}
        assert dispatched["credential_register_after"].type == "openai_api_key"
        assert dispatched["credential_register_after"].credential_id == 7
        assert dispatched["credential_register_after"].scope == "codex"
        assert dispatched["token_register_after"].token_id == 7
        assert dispatched["token_register_after"].agent_type == "codex"

    @patch("agento.framework.cli.credential.get_connection_or_exit")
    @patch("agento.framework.cli.credential._load_framework_config")
    @patch("agento.framework.cli.credential._resolve_credentials")
    @patch("agento.framework.agent_manager.register_credential")
    @patch("agento.framework.event_manager.get_event_manager")
    def test_dispatched_event_carries_oauth_type_for_interactive_flow(
        self,
        mock_get_events,
        mock_register,
        mock_resolve,
        mock_config,
        mock_conn_fn,
    ):
        from agento.framework.cli.credential import CredentialRegisterCommand

        mock_config.return_value = ({}, None, None)
        conn = MagicMock()
        mock_conn_fn.return_value = conn
        mock_resolve.return_value = ({"subscription_key": "sk-new"}, "oauth")
        mock_register.return_value = self._make_registered_token("oauth")
        events = MagicMock()
        mock_get_events.return_value = events

        args = argparse.Namespace(
            scope="codex",
            label="my-label",
            with_api_key=None,
            with_access_token=None,
            token_limit=0,
        )
        CredentialRegisterCommand().execute(args)

        dispatched = {name: payload for name, payload in
                      (c.args for c in events.dispatch.call_args_list)}
        assert dispatched["credential_register_after"].type == "oauth"


class TestRefusesToWriteThroughALease:
    """Both CLI paths that reach ``register_credential``'s upsert must refuse, roll back and
    exit non-zero — never commit and never dispatch a success event. The store's guard is
    tested against real MySQL; what is tested here is that the CLI honours it instead of
    leaving a half-open transaction behind (and, for refresh, that the operator's brand-new
    credential is not written where the leaseholder's own capture would silently undo it)."""

    def _leased(self):
        from agento.framework.agent_manager.errors import CredentialLeasedError
        return CredentialLeasedError("prod-1", "job-118-attempt-1", "2026-08-05T12:31:07")

    @patch("agento.framework.event_manager.get_event_manager")
    @patch("agento.framework.agent_manager.register_credential")
    @patch("agento.framework.cli.credential._resolve_credentials")
    @patch("agento.framework.cli.credential.get_connection_or_exit")
    @patch("agento.framework.cli.credential._load_framework_config")
    def test_credential_register_rolls_back_and_exits(
        self, mock_config, mock_conn_fn, mock_creds, mock_register, mock_events
    ):
        from agento.framework.cli.credential import CredentialRegisterCommand

        mock_config.return_value = ({}, None, None)
        conn = MagicMock()
        mock_conn_fn.return_value = conn
        mock_creds.return_value = ({"subscription_key": "sk"}, "oauth")
        mock_register.side_effect = self._leased()
        events = MagicMock()
        mock_events.return_value = events

        args = argparse.Namespace(
            scope="claude", label="prod-1", token_limit=0,
            with_api_key=False, with_access_token=False,
        )
        with pytest.raises(SystemExit) as exit_info:
            CredentialRegisterCommand().execute(args)

        assert exit_info.value.code == 1
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()
        conn.close.assert_called_once()
        events.dispatch.assert_not_called()

    @patch("agento.framework.event_manager.get_event_manager")
    @patch("agento.framework.agent_manager.register_credential")
    @patch("agento.framework.agent_manager.auth.authenticate_interactive")
    @patch("agento.framework.agent_manager.credential_store.get_credential")
    @patch("agento.framework.cli.credential.get_connection_or_exit")
    @patch("agento.framework.cli.credential._load_framework_config")
    def test_credential_refresh_rolls_back_and_exits(
        self, mock_config, mock_conn_fn, mock_get_credential, mock_auth, mock_register, mock_events
    ):
        from agento.framework.cli.credential import CredentialRefreshCommand

        mock_config.return_value = ({}, None, None)
        # Two connections: the lookup, then the write. Only the second must roll back.
        lookup_conn, write_conn = MagicMock(), MagicMock()
        mock_conn_fn.side_effect = [lookup_conn, write_conn]
        mock_get_credential.return_value = _make_token("claude", 1)
        mock_auth.return_value = MagicMock(
            subscription_key="sk", refresh_token="R1", expires_at=None,
            subscription_type=None, id_token=None, raw_auth={},
        )
        mock_register.side_effect = self._leased()
        events = MagicMock()
        mock_events.return_value = events

        with (
            patch("sys.stdin.isatty", return_value=True),
            pytest.raises(SystemExit) as exit_info,
        ):
            CredentialRefreshCommand().execute(argparse.Namespace(credential_id=1))

        assert exit_info.value.code == 1
        write_conn.rollback.assert_called_once()
        write_conn.commit.assert_not_called()
        write_conn.close.assert_called_once()
        lookup_conn.rollback.assert_not_called()
        events.dispatch.assert_not_called()


class TestCredentialListShowsAccount:
    """AG-45: the real authenticated account is shown next to the label, so a label that
    does not match its account (or two labels on one account) is detectable by an
    operator. ``builtin_harnesses`` (module fixture) registers the claude authenticator."""

    def _claude_row(self, id_, label, email):
        now = datetime.now(UTC).replace(tzinfo=None)
        creds = (
            {"raw_auth": {"claude_json": {"oauthAccount": {"emailAddress": email}}}}
            if email else {"api_key": "sk-ant-x"}
        )
        return CredentialRecord(
            id=id_, scope="claude", type="oauth", label=label, credentials=creds,
            token_limit=0, enabled=True, status=CredentialStatus.OK, priority=0,
            error_msg=None, expires_at=None, used_at=None, created_at=now, updated_at=now,
        )

    def _run(self, rows, *, as_json, capsys):
        from agento.framework.cli.credential import CredentialListCommand
        args = argparse.Namespace(scope=None, all=True, json=as_json)
        with patch("agento.framework.cli.credential._load_framework_config",
                   return_value=(MagicMock(), MagicMock(), MagicMock(usage_window_hours=24))), \
             patch("agento.framework.cli.credential.get_connection_or_exit"), \
             patch("agento.framework.agent_manager.list_credentials", return_value=rows), \
             patch("agento.framework.agent_manager.get_usage_summaries", return_value=[]):
            CredentialListCommand().execute(args)
        return capsys.readouterr().out

    def test_text_shows_oauth_email(self, capsys):
        out = self._run([self._claude_row(1, "prod", "ops@example.com")],
                        as_json=False, capsys=capsys)
        assert "account=ops@example.com" in out

    def test_text_shows_placeholder_for_api_key(self, capsys):
        out = self._run([self._claude_row(1, "prod", None)], as_json=False, capsys=capsys)
        assert "account=?" in out

    def test_json_exposes_account(self, capsys):
        import json
        rows = json.loads(self._run(
            [self._claude_row(1, "a", "ops@example.com"), self._claude_row(2, "b", None)],
            as_json=True, capsys=capsys,
        ))
        by_id = {r["id"]: r for r in rows}
        assert by_id[1]["account"] == "ops@example.com"
        assert by_id[2]["account"] is None


class TestCredentialRegisterWarnsOnDuplicateAccount:
    """AG-45: registering a second label on an oauthAccount that already has one warns —
    the two rows share a single-use refresh token and race on rotation."""

    def _claude_row(self, id_, label, email):
        now = datetime.now(UTC).replace(tzinfo=None)
        return CredentialRecord(
            id=id_, scope="claude", type="oauth", label=label,
            credentials={"raw_auth": {"claude_json": {"oauthAccount": {"emailAddress": email}}}},
            token_limit=0, enabled=True, status=CredentialStatus.OK, priority=0,
            error_msg=None, expires_at=None, used_at=None, created_at=now, updated_at=now,
        )

    def _register(self, incoming_email, existing, *, label, capsys):
        from agento.framework.cli.credential import CredentialRegisterCommand
        incoming = {"raw_auth": {"claude_json": {"oauthAccount": {"emailAddress": incoming_email}}}}
        args = argparse.Namespace(
            scope="claude", label=label, token_limit=0,
            with_api_key=False, with_access_token=False,
        )
        with patch("agento.framework.cli.credential._load_framework_config",
                   return_value=({}, None, None)), \
             patch("agento.framework.cli.credential.get_connection_or_exit",
                   return_value=MagicMock()), \
             patch("agento.framework.cli.credential._resolve_credentials",
                   return_value=(incoming, "oauth")), \
             patch("agento.framework.agent_manager.list_credentials",
                   return_value=existing), \
             patch("agento.framework.agent_manager.register_credential",
                   return_value=self._claude_row(9, label, incoming_email)), \
             patch("agento.framework.event_manager.get_event_manager", return_value=MagicMock()):
            CredentialRegisterCommand().execute(args)
        return capsys.readouterr().err

    def test_warns_when_other_label_holds_same_account(self, capsys):
        err = self._register(
            "ops@example.com", [self._claude_row(1, "prod", "ops@example.com")],
            label="prod-copy", capsys=capsys,
        )
        assert "already registered" in err
        assert "[1] prod" in err

    def test_no_warning_when_reregistering_same_label(self, capsys):
        # Refreshing the same credential (same label) is not a duplicate account.
        err = self._register(
            "ops@example.com", [self._claude_row(1, "prod", "ops@example.com")],
            label="prod", capsys=capsys,
        )
        assert "already registered" not in err

    def test_no_warning_for_distinct_accounts(self, capsys):
        err = self._register(
            "new@example.com", [self._claude_row(1, "prod", "ops@example.com")],
            label="second", capsys=capsys,
        )
        assert "already registered" not in err
