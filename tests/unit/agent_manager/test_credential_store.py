from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from agento.framework.agent_manager.credential_store import (
    RefreshLease,
    clear_auto_credential_error,
    clear_credential_error,
    count_credentials_for_scope,
    deregister_credential,
    earliest_lease_expiry_for_scope,
    earliest_throttle_reset_for_scope,
    get_credential,
    list_credentials,
    mark_credential_error,
    register_credential,
    release_credential_lease,
    renew_credential_leases,
    select_credential,
    set_credential_priority,
    throttle_credential,
    update_refreshed_credentials,
)
from agento.framework.agent_manager.errors import CredentialLeasedError
from agento.framework.agent_manager.models import CredentialRecord, CredentialStatus


def _mock_conn(fetchone_return=None, fetchall_return=None, lastrowid=1, rowcount=1):
    """Create a mock pymysql Connection with cursor context manager."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    cursor.fetchall.return_value = fetchall_return or []
    cursor.lastrowid = lastrowid
    cursor.rowcount = rowcount
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


def _mock_conn_with_fetches(fetchone_seq, fetchone_seq_scalar=None):
    """Mock connection where fetchone returns successive values from a sequence."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.side_effect = fetchone_seq
    cursor.lastrowid = 1
    cursor.rowcount = 1
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


_ENCRYPTED_BLOB = "aes256:deadbeef:cafebabe"
_PLAINTEXT_CREDS = {"subscription_key": "sk-test"}

_SAMPLE_ROW = {
    "id": 1,
    "agent_type": "claude",
    "scope": "claude",
    "type": "oauth",
    "label": "prod-1",
    "credentials": _ENCRYPTED_BLOB,
    "token_limit": 100000,
    "priority": 0,
    "enabled": True,
    "status": "ok",
    "error_msg": None,
    "expires_at": None,
    "error_source": None,
    "lease_owner": None,
    "leased_until": None,
    # register_credential's pre-flight lock computes this in SQL; a free row reports 0.
    "lease_active": 0,
    "used_at": None,
    "created_at": "2025-01-01 00:00:00",
    "updated_at": "2025-01-01 00:00:00",
}


class _FakeEncryptor:
    def encrypt(self, plaintext: str) -> str:
        return f"aes256:iv:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        import json
        if ciphertext == _ENCRYPTED_BLOB:
            return json.dumps(_PLAINTEXT_CREDS)
        return ciphertext.split(":", 2)[-1]


@pytest.fixture(autouse=True)
def _fake_encryptor(monkeypatch):
    from agento.framework import encryptor as enc
    monkeypatch.setattr(enc, "_instance", _FakeEncryptor())
    yield


class TestRegisterToken:
    def test_returns_token_from_inserted_row(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW, lastrowid=1)

        token = register_credential(
            conn,
            scope="claude",
            label="prod-1",
            credentials=_PLAINTEXT_CREDS,
            token_limit=100000,
        )

        assert isinstance(token, CredentialRecord)
        assert token.id == 1
        assert token.scope == "claude"
        assert token.label == "prod-1"
        assert token.credentials == _PLAINTEXT_CREDS
        assert cursor.execute.call_count == 3  # lease pre-flight + INSERT + SELECT

    def test_passes_encrypted_credentials(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW)

        register_credential(conn, "codex", "codex-1", _PLAINTEXT_CREDS, 50000)

        # [0] is the lease pre-flight SELECT ... FOR UPDATE; the INSERT is [1].
        insert_call = cursor.execute.call_args_list[1]
        assert "INSERT INTO credential" in insert_call[0][0]
        assert "credentials" in insert_call[0][0]
        params = insert_call[0][1]
        # INSERT dual-writes the scope into both ``scope`` and the legacy
        # ``agent_type`` column, so the type moves one slot right.
        assert params[0] == "codex"   # scope
        assert params[1] == "codex"   # agent_type (dual-written for one release)
        assert params[2] == "oauth"   # type (default)
        assert params[3] == "codex-1" # label
        assert params[4].startswith("aes256:")  # encrypted credentials
        assert params[5] == 50000     # token_limit

    def test_register_resets_status_and_clears_error_on_refresh(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW)

        register_credential(conn, "claude", "prod-1", _PLAINTEXT_CREDS)

        insert_sql = cursor.execute.call_args_list[1][0][0]
        assert "status" in insert_sql and "'ok'" in insert_sql
        assert "error_msg" in insert_sql and "NULL" in insert_sql
        # Provenance is void once an operator re-states that the credential is good.
        assert "error_source = NULL" in insert_sql

    def test_pulls_expires_at_from_credentials_epoch(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW)

        creds = {"subscription_key": "sk-test", "expires_at": 1893456000}
        register_credential(conn, "claude", "prod-1", creds)

        params = cursor.execute.call_args_list[1][0][1]
        assert params[-1] == datetime(2030, 1, 1, 0, 0, 0)

    def test_pulls_expires_at_from_credentials_iso(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW)

        creds = {"subscription_key": "sk-test", "expires_at": "2030-06-01T12:34:56Z"}
        register_credential(conn, "claude", "prod-1", creds)

        params = cursor.execute.call_args_list[1][0][1]
        assert params[-1] == datetime(2030, 6, 1, 12, 34, 56)

    def test_malformed_expires_at_becomes_null(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW)

        creds = {"subscription_key": "sk-test", "expires_at": "not-a-date"}
        register_credential(conn, "claude", "prod-1", creds)

        params = cursor.execute.call_args_list[1][0][1]
        assert params[-1] is None


class TestDeregisterToken:
    def test_returns_true_when_found(self):
        conn, cursor = _mock_conn(rowcount=1)

        result = deregister_credential(conn, credential_id=5)

        assert result is True
        sql = cursor.execute.call_args[0][0]
        assert "UPDATE credential SET enabled = FALSE" in sql

    def test_returns_false_when_not_found(self):
        conn, _cursor = _mock_conn(rowcount=0)

        result = deregister_credential(conn, credential_id=999)

        assert result is False


class TestListTokens:
    def test_returns_all_enabled(self):
        conn, cursor = _mock_conn(
            fetchall_return=[_SAMPLE_ROW, {**_SAMPLE_ROW, "id": 2, "label": "prod-2"}],
        )

        tokens = list_credentials(conn)

        assert len(tokens) == 2
        sql = cursor.execute.call_args[0][0]
        assert "enabled = TRUE" in sql

    def test_filter_by_agent_type(self):
        conn, cursor = _mock_conn(fetchall_return=[_SAMPLE_ROW])

        list_credentials(conn, scope="claude")

        sql = cursor.execute.call_args[0][0]
        assert "scope = %s" in sql
        params = cursor.execute.call_args[0][1]
        assert "claude" in params

    def test_include_disabled(self):
        conn, cursor = _mock_conn(fetchall_return=[])

        list_credentials(conn, enabled_only=False)

        sql = cursor.execute.call_args[0][0]
        assert "enabled = TRUE" not in sql


class TestGetToken:
    def test_returns_token_when_found(self):
        conn, _cursor = _mock_conn(fetchone_return=_SAMPLE_ROW)

        token = get_credential(conn, credential_id=1)

        assert token is not None
        assert token.id == 1

    def test_returns_none_when_not_found(self):
        conn, _cursor = _mock_conn(fetchone_return=None)

        token = get_credential(conn, credential_id=999)

        assert token is None


class TestSelectToken:
    def test_selects_lru_healthy_and_stamps_used_at(self):
        conn, cursor = _mock_conn_with_fetches(
            [{"id": 1}, _SAMPLE_ROW],
        )

        token = select_credential(conn, "claude")

        assert token is not None
        assert token.id == 1
        assert cursor.execute.call_count == 3
        select_sql = cursor.execute.call_args_list[0][0][0]
        # Blocking FOR UPDATE (NOT SKIP LOCKED): concurrent claimants must serialize
        # on the row lock so two workers never receive the same token. SKIP LOCKED
        # over the filesorted pool scan hands the same row to two claimants — see
        # tests/integration/test_token_pool_concurrency.py.
        assert "FOR UPDATE" in select_sql
        assert "SKIP LOCKED" not in select_sql
        assert "status = 'ok'" in select_sql
        assert "expires_at IS NULL OR expires_at > UTC_TIMESTAMP()" in select_sql
        # Rows held by a live refresh lease are excluded for EVERY caller, leased or not.
        assert "leased_until IS NULL OR leased_until <= UTC_TIMESTAMP()" in select_sql
        update_sql = cursor.execute.call_args_list[1][0][0]
        assert "SET used_at = UTC_TIMESTAMP(6)" in update_sql
        # No lease requested -> the update binds no owner and clears stale lease metadata.
        assert "lease_owner = NULL" in update_sql
        assert cursor.execute.call_args_list[1][0][1] == (1,)
        conn.commit.assert_called()

    def test_returns_none_when_no_healthy_token(self):
        conn, _cursor = _mock_conn(fetchone_return=None)

        token = select_credential(conn, "codex")

        assert token is None
        conn.commit.assert_called()

    def test_orders_nulls_first(self):
        conn, _cursor = _mock_conn_with_fetches([{"id": 1}, _SAMPLE_ROW])

        select_credential(conn, "claude")

        select_sql = _cursor.execute.call_args_list[0][0][0]
        assert "used_at IS NULL DESC" in select_sql

    def test_filters_by_agent_type(self):
        conn, cursor = _mock_conn_with_fetches([{"id": 1}, _SAMPLE_ROW])

        select_credential(conn, "codex")

        # The locking SELECT still binds exactly the provider (statement 0 of 3).
        params = cursor.execute.call_args_list[0][0][1]
        assert params == ("codex",)


class TestMarkAndClearTokenError:
    def test_mark_credential_error_sets_status_and_msg(self):
        conn, cursor = _mock_conn(rowcount=1)

        result = mark_credential_error(conn, 7, "OAuth expired")

        assert result is True
        sql = cursor.execute.call_args[0][0]
        assert "status = 'error'" in sql
        params = cursor.execute.call_args[0][1]
        # Provenance defaults to 'operator' — fail-closed, so a caller that forgets to
        # say source="auto" gets the stickier state rather than the self-clearing one.
        assert params == ("OAuth expired", "operator", 7)

    def test_mark_token_error_records_auto_provenance_when_asked(self):
        conn, cursor = _mock_conn(rowcount=1)

        mark_credential_error(conn, 7, "401", source="auto")

        assert cursor.execute.call_args[0][1] == ("401", "auto", 7)

    def test_mark_token_error_rejects_an_unknown_source(self):
        conn, cursor = _mock_conn(rowcount=1)

        with pytest.raises(ValueError):
            mark_credential_error(conn, 7, "401", source="whatever")
        cursor.execute.assert_not_called()

    def test_mark_credential_error_truncates_long_message(self):
        conn, cursor = _mock_conn(rowcount=1)

        mark_credential_error(conn, 7, "x" * 5000)

        params = cursor.execute.call_args[0][1]
        assert len(params[0]) == 1000

    def test_mark_credential_error_returns_false_when_not_found(self):
        conn, _cursor = _mock_conn(rowcount=0)

        assert mark_credential_error(conn, 999, "msg") is False

    def test_clear_credential_error_resets_status(self):
        conn, cursor = _mock_conn(rowcount=1)

        result = clear_credential_error(conn, 7)

        assert result is True
        sql = cursor.execute.call_args[0][0]
        assert "status = 'ok'" in sql
        assert "error_msg = NULL" in sql
        # Operator recovery also lifts any usage-limit throttle.
        assert "throttled_until = NULL" in sql
        assert "error_source = NULL" in sql
        # Must NOT free a live refresh lease — that hands the row to a second worker
        # mid-refresh and reproduces the incident on demand.
        assert "lease_owner" not in sql

    def test_clear_credential_error_returns_false_when_not_found(self):
        conn, _cursor = _mock_conn(rowcount=0)

        assert clear_credential_error(conn, 999) is False


class TestClearAutoTokenError:
    def test_only_clears_the_frameworks_own_quarantine(self):
        conn, cursor = _mock_conn(rowcount=1)

        assert clear_auto_credential_error(conn, 7) is True
        sql = cursor.execute.call_args[0][0]
        assert "status = 'ok'" in sql
        # The predicate is what keeps an operator decision — and a pre-030 row whose
        # provenance is unknown (NULL) — untouchable.
        assert "error_source = 'auto'" in sql
        assert cursor.execute.call_args[0][1] == (7,)

    def test_returns_false_when_nothing_matched(self):
        conn, _cursor = _mock_conn(rowcount=0)

        assert clear_auto_credential_error(conn, 7) is False


class TestRefreshLease:
    def test_select_token_folds_the_lease_into_the_used_at_update(self):
        leased_row = {**_SAMPLE_ROW, "lease_owner": "job-7-attempt-1", "leased_until": None}
        conn, cursor = _mock_conn_with_fetches([_SAMPLE_ROW, leased_row])

        token = select_credential(
            conn,
            "claude",
            RefreshLease(owner="job-7-attempt-1", ttl_seconds=300, should_lease=lambda _t: True),
        )

        # Still exactly 3 statements, with the lease written by the middle one — so the
        # row materialized by the final SELECT already carries it and the returned Token
        # is truthful enough for the owner-checked release to fire.
        assert cursor.execute.call_count == 3
        update_sql, update_params = cursor.execute.call_args_list[1][0]
        assert "lease_owner = %s" in update_sql
        assert "leased_until = UTC_TIMESTAMP() + INTERVAL %s SECOND" in update_sql
        assert update_params == ("job-7-attempt-1", 300, 1)
        assert token.lease_owner == "job-7-attempt-1"

    def test_a_declining_policy_clears_stale_lease_metadata(self):
        conn, cursor = _mock_conn_with_fetches([_SAMPLE_ROW, _SAMPLE_ROW])

        select_credential(
            conn,
            "claude",
            RefreshLease(owner="job-7-attempt-1", ttl_seconds=300, should_lease=lambda _t: False),
        )

        update_sql, update_params = cursor.execute.call_args_list[1][0]
        assert "lease_owner = NULL" in update_sql and "leased_until = NULL" in update_sql
        assert update_params == (1,)

    def test_the_policy_sees_a_decrypted_token(self):
        seen = []
        conn, _cursor = _mock_conn_with_fetches([_SAMPLE_ROW, _SAMPLE_ROW])

        def _policy(candidate):
            seen.append(candidate)
            return False

        select_credential(
            conn,
            "claude",
            RefreshLease(owner="o", ttl_seconds=300, should_lease=_policy),
        )

        # credentials is AES ciphertext in the row; a policy fed the raw row would
        # inspect a base64 string and .get("refresh_token") would raise.
        assert isinstance(seen[0], CredentialRecord)
        assert seen[0].credentials == _PLAINTEXT_CREDS

    def test_release_is_owner_checked(self):
        conn, cursor = _mock_conn(rowcount=1)

        assert release_credential_lease(conn, 7, "job-7-attempt-2") is True
        sql, params = cursor.execute.call_args[0]
        assert "lease_owner = NULL" in sql and "WHERE id = %s AND lease_owner = %s" in sql
        assert params == (7, "job-7-attempt-2")

    def test_release_by_a_stale_owner_touches_nothing(self):
        conn, _cursor = _mock_conn(rowcount=0)

        assert release_credential_lease(conn, 7, "job-7-attempt-1") is False

    def test_renew_binds_every_owner(self):
        conn, cursor = _mock_conn(rowcount=2)

        assert renew_credential_leases(conn, ["a", "b"], 300) == 2
        sql, params = cursor.execute.call_args[0]
        assert "lease_owner IN (%s, %s)" in sql
        assert params == (300, "a", "b")

    def test_renew_with_no_owners_emits_no_sql(self):
        conn, cursor = _mock_conn(rowcount=0)

        # `IN ()` is a MySQL syntax error, so an empty set must short-circuit.
        assert renew_credential_leases(conn, [], 300) == 0
        cursor.execute.assert_not_called()

    def test_register_token_refuses_to_write_through_a_live_lease(self):
        conn, cursor = _mock_conn(
            fetchone_return={
                "lease_owner": "job-7-attempt-1",
                "leased_until": datetime(2026, 7, 15, 16, 31, 7),
                "lease_active": 1,
            },
        )

        with pytest.raises(CredentialLeasedError) as excinfo:
            register_credential(conn, "claude", "prod-1", _PLAINTEXT_CREDS)

        # Nothing written: the refusal happens before the upsert, in the same transaction
        # as the lock, so a lease taken concurrently is either seen or loses the row lock.
        assert cursor.execute.call_count == 1
        assert "FOR UPDATE" in cursor.execute.call_args[0][0]
        assert excinfo.value.lease_owner == "job-7-attempt-1"

    def test_register_token_writes_through_an_expired_lease(self):
        conn, cursor = _mock_conn(
            fetchone_return={**_SAMPLE_ROW, "lease_owner": "job-7-attempt-1", "lease_active": 0},
        )

        register_credential(conn, "claude", "prod-1", _PLAINTEXT_CREDS)

        insert_sql = cursor.execute.call_args_list[1][0][0]
        # Reachable only when no lease is ACTIVE, so clearing an expired holder here is
        # safe — and keeps credential:list from showing a long-dead one.
        assert "lease_owner  = NULL" in insert_sql


class TestThrottleToken:
    def test_sets_throttled_until_without_poisoning(self):
        conn, cursor = _mock_conn(rowcount=1)
        until = datetime(2026, 7, 22, 12, 0, 0)

        result = throttle_credential(conn, 7, until, "hit your session limit")

        assert result is True
        sql = cursor.execute.call_args[0][0]
        # Cooldown only — must NOT touch status or expires_at (credential expiry).
        assert "throttled_until = %s" in sql
        assert "status" not in sql
        assert "expires_at" not in sql
        params = cursor.execute.call_args[0][1]
        assert params == (until, 7)

    def test_returns_false_when_not_found(self):
        conn, _cursor = _mock_conn(rowcount=0)
        assert throttle_credential(conn, 999, datetime(2026, 7, 22, 12, 0), "msg") is False


class TestSelectTokenSkipsThrottled:
    def test_select_credential_query_excludes_throttled(self):
        conn, cursor = _mock_conn(fetchone_return=None)

        select_credential(conn, "claude")

        sql = cursor.execute.call_args_list[0][0][0]
        assert "throttled_until IS NULL OR throttled_until <= UTC_TIMESTAMP()" in sql


class TestCountTokensForProvider:
    def test_returns_total_and_healthy(self):
        conn, cursor = _mock_conn_with_fetches([{"c": 3}, {"c": 1}])

        total, healthy = count_credentials_for_scope(conn, "claude")

        assert total == 3
        assert healthy == 1
        assert cursor.execute.call_count == 2
        healthy_sql = cursor.execute.call_args_list[1][0][0]
        assert "status = 'ok'" in healthy_sql
        assert "expires_at IS NULL OR expires_at > UTC_TIMESTAMP()" in healthy_sql

    def test_zero_when_no_tokens(self):
        conn, _cursor = _mock_conn_with_fetches([{"c": 0}, {"c": 0}])

        total, healthy = count_credentials_for_scope(conn, "codex")

        assert total == 0
        assert healthy == 0


class TestEarliestThrottleResetForScope:
    def test_returns_min_throttled_until(self):
        reset = datetime(2026, 8, 27, 15, 0, 0)
        conn, cursor = _mock_conn(fetchone_return={"reset": reset})

        got = earliest_throttle_reset_for_scope(conn, "claude")

        assert got == reset
        sql = cursor.execute.call_args_list[-1][0][0]
        # Only tokens that heal on their own: enabled, ok, unexpired, and throttled
        # into the future — never a poisoned or expired row.
        assert "MIN(throttled_until)" in sql
        assert "status = 'ok'" in sql
        assert "throttled_until > UTC_TIMESTAMP()" in sql

    def test_returns_none_when_nothing_throttled(self):
        conn, _cursor = _mock_conn(fetchone_return={"reset": None})

        assert earliest_throttle_reset_for_scope(conn, "codex") is None


class TestEarliestLeaseExpiryForScope:
    def test_returns_min_leased_until(self):
        expiry = datetime(2026, 8, 28, 12, 5, 0)
        conn, cursor = _mock_conn(fetchone_return={"reset": expiry})

        got = earliest_lease_expiry_for_scope(conn, "claude")

        assert got == expiry
        sql = cursor.execute.call_args_list[-1][0][0]
        # Only healthy tokens whose lease frees them on its own: enabled, ok, unexpired,
        # and leased into the future — never a poisoned or expired row.
        assert "MIN(leased_until)" in sql
        assert "status = 'ok'" in sql
        assert "leased_until > UTC_TIMESTAMP()" in sql

    def test_returns_none_when_nothing_leased(self):
        conn, _cursor = _mock_conn(fetchone_return={"reset": None})

        assert earliest_lease_expiry_for_scope(conn, "codex") is None


class TestCredentialStatusMapping:
    def test_from_row_with_ok(self):
        conn, _cursor = _mock_conn(fetchone_return={**_SAMPLE_ROW, "status": "ok"})
        token = get_credential(conn, 1)
        assert token.status == CredentialStatus.OK

    def test_from_row_with_error(self):
        conn, _cursor = _mock_conn(
            fetchone_return={**_SAMPLE_ROW, "status": "error", "error_msg": "expired"},
        )
        token = get_credential(conn, 1)
        assert token.status == CredentialStatus.ERROR
        assert token.error_msg == "expired"


class TestRegisterTokenType:
    def test_register_credential_persists_type_default_oauth(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW, lastrowid=1)

        register_credential(conn, "codex", "lbl", {"subscription_key": "x"})

        insert_call = cursor.execute.call_args_list[1]
        insert_sql = insert_call[0][0]
        params = insert_call[0][1]
        assert "type" in insert_sql
        # Param tuple: (scope, agent_type, type, label, encrypted, token_limit, expires_at)
        assert params[2] == "oauth"

    def test_register_credential_persists_type_codex_access_token(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW, lastrowid=1)

        register_credential(conn, "codex", "lbl", {"subscription_key": "x"}, type="codex_access_token")

        insert_call = cursor.execute.call_args_list[1]
        params = insert_call[0][1]
        # Param tuple: (agent_type, type, label, encrypted, token_limit, expires_at)
        assert params[2] == "codex_access_token"


class TestSelectTokenPriority:
    def test_select_orders_by_priority_then_used_at(self):
        conn, cursor = _mock_conn_with_fetches([{"id": 1}, _SAMPLE_ROW])

        select_credential(conn, "claude")

        select_sql = cursor.execute.call_args_list[0][0][0]
        # priority ASC must appear in ORDER BY and before used_at
        assert "priority ASC" in select_sql
        priority_pos = select_sql.index("priority ASC")
        used_at_pos = select_sql.index("used_at IS NULL DESC")
        assert priority_pos < used_at_pos

    def test_rapid_same_priority_claims_rotate_across_tokens(self):
        base = {
            **_SAMPLE_ROW,
            "credentials": None,
            "priority": 0,
        }
        rows = [
            {**base, "id": 1, "label": "oauth-a"},
            {**base, "id": 2, "label": "oauth-b"},
            {**base, "id": 3, "label": "api-key", "type": "anthropic_api_key"},
        ]

        conn = _RotatingTokenConnection(rows)

        claims = [select_credential(conn, "claude").id for _ in range(10)]

        assert claims == [1, 2, 3, 1, 2, 3, 1, 2, 3, 1]
        assert {token_id: claims.count(token_id) for token_id in {1, 2, 3}} == {
            1: 4,
            2: 3,
            3: 3,
        }


class TestSetTokenPriority:
    def test_set_priority_updates_row(self):
        conn, cursor = _mock_conn(rowcount=1)

        result = set_credential_priority(conn, 42, 5)

        assert result is True
        sql = cursor.execute.call_args[0][0]
        assert "UPDATE credential SET priority" in sql
        params = cursor.execute.call_args[0][1]
        assert params == (5, 42)

    def test_set_priority_returns_false_when_token_not_found(self):
        conn, _cursor = _mock_conn(rowcount=0)

        result = set_credential_priority(conn, 999, 0)

        assert result is False


class _RotatingTokenConnection:
    def __init__(self, rows):
        self.rows = {row["id"]: dict(row) for row in rows}
        self._tick = 0
        self.commit = MagicMock()
        self._cursor = _RotatingTokenCursor(self)

    def cursor(self):
        return self._cursor


class _RotatingTokenCursor:
    def __init__(self, conn: _RotatingTokenConnection):
        self.conn = conn
        self._result = None
        self._selected_id = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        # The locking SELECT projects the whole row now (the lease policy needs a
        # decrypted CredentialRecord, not an id).
        if "SELECT * FROM credential\n             WHERE scope = %s" in sql:
            scope = params[0]
            candidates = [
                row for row in self.conn.rows.values()
                if row["scope"] == scope
                and row["enabled"] is True
                and row["status"] == "ok"
            ]
            candidates.sort(key=lambda row: (
                row["priority"],
                row["used_at"] is not None,
                row["used_at"] or datetime.min,
                row["id"],
            ))
            self._selected_id = candidates[0]["id"] if candidates else None
            self._result = self.conn.rows[self._selected_id] if self._selected_id else None
            return
        if "UPDATE credential SET used_at = UTC_TIMESTAMP(6)" in sql:
            # These rows carry credentials=None, so no lease policy can fire — the update
            # must be the lease-CLEARING form. A lease-taking update (one that binds an
            # owner) here would mean select_credential leased a row it had no business
            # leasing.
            assert "lease_owner = %s" not in sql, f"unexpected lease-taking update: {sql}"
            token_id = params[0]
            self.conn._tick += 1
            self.conn.rows[token_id]["used_at"] = datetime(
                2026, 1, 1, 0, 0, 0, self.conn._tick,
            )
            self._result = None
            return
        if "SELECT * FROM credential WHERE id = %s" in sql:
            self._result = self.conn.rows[params[0]]
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._result


class TestUpdateRefreshedCredentials:
    def test_targeted_update_by_id_preserves_operator_state(self):
        conn, cursor = _mock_conn()
        update_refreshed_credentials(
            conn, 7, {"subscription_key": "sk-new", "expires_at": None}
        )
        sql, params = cursor.execute.call_args[0]
        assert "UPDATE credential" in sql
        assert "WHERE id = %s" in sql
        # Must never touch operator/health/identity columns (only credentials,
        # expires_at, updated_at). "credential" contains none of these as substrings.
        for col in ("enabled", "status", "error_msg", "priority", "label", "type", "token_limit"):
            assert col not in sql
        assert params[-1] == 7                 # bound id (last param)
        assert params[0].startswith("aes256:")  # credentials encrypted, not plaintext

    def test_refreshes_expires_at_from_seconds_payload(self):
        conn, cursor = _mock_conn()
        update_refreshed_credentials(conn, 7, {"expires_at": 1799999999})
        _sql, params = cursor.execute.call_args[0]
        assert isinstance(params[1], datetime)   # parity with register_credential

    def test_none_expires_at_writes_null(self):
        conn, cursor = _mock_conn()
        update_refreshed_credentials(conn, 7, {"expires_at": None})
        _sql, params = cursor.execute.call_args[0]
        assert params[1] is None

    def test_does_not_commit_caller_owns_transaction(self):
        conn, _ = _mock_conn()
        update_refreshed_credentials(conn, 7, {"x": 1})
        conn.commit.assert_not_called()
