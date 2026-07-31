from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from agento.framework.agent_manager.models import AgentProvider, Token, TokenStatus
from agento.framework.agent_manager.token_store import (
    clear_token_error,
    count_tokens_for_provider,
    deregister_token,
    get_token,
    list_tokens,
    mark_token_error,
    register_token,
    select_token,
    set_token_priority,
    throttle_token,
    update_refreshed_credentials,
)


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
    "type": "oauth",
    "label": "prod-1",
    "credentials": _ENCRYPTED_BLOB,
    "token_limit": 100000,
    "priority": 0,
    "enabled": True,
    "status": "ok",
    "error_msg": None,
    "expires_at": None,
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

        token = register_token(
            conn,
            agent_type=AgentProvider.CLAUDE,
            label="prod-1",
            credentials=_PLAINTEXT_CREDS,
            token_limit=100000,
        )

        assert isinstance(token, Token)
        assert token.id == 1
        assert token.agent_type == AgentProvider.CLAUDE
        assert token.label == "prod-1"
        assert token.credentials == _PLAINTEXT_CREDS
        assert cursor.execute.call_count == 2  # INSERT + SELECT

    def test_passes_encrypted_credentials(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW)

        register_token(conn, AgentProvider.CODEX, "codex-1", _PLAINTEXT_CREDS, 50000)

        insert_call = cursor.execute.call_args_list[0]
        assert "INSERT INTO oauth_token" in insert_call[0][0]
        assert "credentials" in insert_call[0][0]
        params = insert_call[0][1]
        assert params[0] == "codex"   # agent_type
        assert params[1] == "oauth"   # type (default)
        assert params[2] == "codex-1" # label
        assert params[3].startswith("aes256:")  # encrypted credentials
        assert params[4] == 50000     # token_limit

    def test_register_resets_status_and_clears_error_on_refresh(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW)

        register_token(conn, AgentProvider.CLAUDE, "prod-1", _PLAINTEXT_CREDS)

        insert_sql = cursor.execute.call_args_list[0][0][0]
        assert "status" in insert_sql and "'ok'" in insert_sql
        assert "error_msg" in insert_sql and "NULL" in insert_sql

    def test_pulls_expires_at_from_credentials_epoch(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW)

        creds = {"subscription_key": "sk-test", "expires_at": 1893456000}
        register_token(conn, AgentProvider.CLAUDE, "prod-1", creds)

        params = cursor.execute.call_args_list[0][0][1]
        assert params[-1] == datetime(2030, 1, 1, 0, 0, 0)

    def test_pulls_expires_at_from_credentials_iso(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW)

        creds = {"subscription_key": "sk-test", "expires_at": "2030-06-01T12:34:56Z"}
        register_token(conn, AgentProvider.CLAUDE, "prod-1", creds)

        params = cursor.execute.call_args_list[0][0][1]
        assert params[-1] == datetime(2030, 6, 1, 12, 34, 56)

    def test_malformed_expires_at_becomes_null(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW)

        creds = {"subscription_key": "sk-test", "expires_at": "not-a-date"}
        register_token(conn, AgentProvider.CLAUDE, "prod-1", creds)

        params = cursor.execute.call_args_list[0][0][1]
        assert params[-1] is None


class TestDeregisterToken:
    def test_returns_true_when_found(self):
        conn, cursor = _mock_conn(rowcount=1)

        result = deregister_token(conn, token_id=5)

        assert result is True
        sql = cursor.execute.call_args[0][0]
        assert "UPDATE oauth_token SET enabled = FALSE" in sql

    def test_returns_false_when_not_found(self):
        conn, _cursor = _mock_conn(rowcount=0)

        result = deregister_token(conn, token_id=999)

        assert result is False


class TestListTokens:
    def test_returns_all_enabled(self):
        conn, cursor = _mock_conn(
            fetchall_return=[_SAMPLE_ROW, {**_SAMPLE_ROW, "id": 2, "label": "prod-2"}],
        )

        tokens = list_tokens(conn)

        assert len(tokens) == 2
        sql = cursor.execute.call_args[0][0]
        assert "enabled = TRUE" in sql

    def test_filter_by_agent_type(self):
        conn, cursor = _mock_conn(fetchall_return=[_SAMPLE_ROW])

        list_tokens(conn, agent_type=AgentProvider.CLAUDE)

        sql = cursor.execute.call_args[0][0]
        assert "agent_type = %s" in sql
        params = cursor.execute.call_args[0][1]
        assert "claude" in params

    def test_include_disabled(self):
        conn, cursor = _mock_conn(fetchall_return=[])

        list_tokens(conn, enabled_only=False)

        sql = cursor.execute.call_args[0][0]
        assert "enabled = TRUE" not in sql


class TestGetToken:
    def test_returns_token_when_found(self):
        conn, _cursor = _mock_conn(fetchone_return=_SAMPLE_ROW)

        token = get_token(conn, token_id=1)

        assert token is not None
        assert token.id == 1

    def test_returns_none_when_not_found(self):
        conn, _cursor = _mock_conn(fetchone_return=None)

        token = get_token(conn, token_id=999)

        assert token is None


class TestSelectToken:
    def test_selects_lru_healthy_and_stamps_used_at(self):
        conn, cursor = _mock_conn_with_fetches(
            [{"id": 1}, _SAMPLE_ROW],
        )

        token = select_token(conn, AgentProvider.CLAUDE)

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
        update_sql = cursor.execute.call_args_list[1][0][0]
        assert "SET used_at = UTC_TIMESTAMP(6)" in update_sql
        conn.commit.assert_called()

    def test_returns_none_when_no_healthy_token(self):
        conn, _cursor = _mock_conn(fetchone_return=None)

        token = select_token(conn, AgentProvider.CODEX)

        assert token is None
        conn.commit.assert_called()

    def test_orders_nulls_first(self):
        conn, _cursor = _mock_conn_with_fetches([{"id": 1}, _SAMPLE_ROW])

        select_token(conn, AgentProvider.CLAUDE)

        select_sql = _cursor.execute.call_args_list[0][0][0]
        assert "used_at IS NULL DESC" in select_sql

    def test_filters_by_agent_type(self):
        conn, cursor = _mock_conn_with_fetches([{"id": 1}, _SAMPLE_ROW])

        select_token(conn, AgentProvider.CODEX)

        params = cursor.execute.call_args_list[0][0][1]
        assert params == ("codex",)


class TestMarkAndClearTokenError:
    def test_mark_token_error_sets_status_and_msg(self):
        conn, cursor = _mock_conn(rowcount=1)

        result = mark_token_error(conn, 7, "OAuth expired")

        assert result is True
        sql = cursor.execute.call_args[0][0]
        assert "status = 'error'" in sql
        params = cursor.execute.call_args[0][1]
        assert params == ("OAuth expired", 7)

    def test_mark_token_error_truncates_long_message(self):
        conn, cursor = _mock_conn(rowcount=1)

        mark_token_error(conn, 7, "x" * 5000)

        params = cursor.execute.call_args[0][1]
        assert len(params[0]) == 1000

    def test_mark_token_error_returns_false_when_not_found(self):
        conn, _cursor = _mock_conn(rowcount=0)

        assert mark_token_error(conn, 999, "msg") is False

    def test_clear_token_error_resets_status(self):
        conn, cursor = _mock_conn(rowcount=1)

        result = clear_token_error(conn, 7)

        assert result is True
        sql = cursor.execute.call_args[0][0]
        assert "status = 'ok'" in sql
        assert "error_msg = NULL" in sql
        # Operator recovery also lifts any usage-limit throttle.
        assert "throttled_until = NULL" in sql

    def test_clear_token_error_returns_false_when_not_found(self):
        conn, _cursor = _mock_conn(rowcount=0)

        assert clear_token_error(conn, 999) is False


class TestThrottleToken:
    def test_sets_throttled_until_without_poisoning(self):
        conn, cursor = _mock_conn(rowcount=1)
        until = datetime(2026, 7, 22, 12, 0, 0)

        result = throttle_token(conn, 7, until, "hit your session limit")

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
        assert throttle_token(conn, 999, datetime(2026, 7, 22, 12, 0), "msg") is False


class TestSelectTokenSkipsThrottled:
    def test_select_token_query_excludes_throttled(self):
        conn, cursor = _mock_conn(fetchone_return=None)

        select_token(conn, AgentProvider.CLAUDE)

        sql = cursor.execute.call_args_list[0][0][0]
        assert "throttled_until IS NULL OR throttled_until <= UTC_TIMESTAMP()" in sql


class TestCountTokensForProvider:
    def test_returns_total_and_healthy(self):
        conn, cursor = _mock_conn_with_fetches([{"c": 3}, {"c": 1}])

        total, healthy = count_tokens_for_provider(conn, AgentProvider.CLAUDE)

        assert total == 3
        assert healthy == 1
        assert cursor.execute.call_count == 2
        healthy_sql = cursor.execute.call_args_list[1][0][0]
        assert "status = 'ok'" in healthy_sql
        assert "expires_at IS NULL OR expires_at > UTC_TIMESTAMP()" in healthy_sql

    def test_zero_when_no_tokens(self):
        conn, _cursor = _mock_conn_with_fetches([{"c": 0}, {"c": 0}])

        total, healthy = count_tokens_for_provider(conn, AgentProvider.CODEX)

        assert total == 0
        assert healthy == 0


class TestTokenStatusMapping:
    def test_from_row_with_ok(self):
        conn, _cursor = _mock_conn(fetchone_return={**_SAMPLE_ROW, "status": "ok"})
        token = get_token(conn, 1)
        assert token.status == TokenStatus.OK

    def test_from_row_with_error(self):
        conn, _cursor = _mock_conn(
            fetchone_return={**_SAMPLE_ROW, "status": "error", "error_msg": "expired"},
        )
        token = get_token(conn, 1)
        assert token.status == TokenStatus.ERROR
        assert token.error_msg == "expired"


class TestRegisterTokenType:
    def test_register_token_persists_type_default_oauth(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW, lastrowid=1)

        register_token(conn, AgentProvider.CODEX, "lbl", {"subscription_key": "x"})

        insert_call = cursor.execute.call_args_list[0]
        insert_sql = insert_call[0][0]
        params = insert_call[0][1]
        assert "type" in insert_sql
        # Param tuple: (agent_type, type, label, encrypted, token_limit, expires_at)
        assert params[1] == "oauth"

    def test_register_token_persists_type_codex_access_token(self):
        conn, cursor = _mock_conn(fetchone_return=_SAMPLE_ROW, lastrowid=1)

        register_token(conn, AgentProvider.CODEX, "lbl", {"subscription_key": "x"}, type="codex_access_token")

        insert_call = cursor.execute.call_args_list[0]
        params = insert_call[0][1]
        # Param tuple: (agent_type, type, label, encrypted, token_limit, expires_at)
        assert params[1] == "codex_access_token"


class TestSelectTokenPriority:
    def test_select_orders_by_priority_then_used_at(self):
        conn, cursor = _mock_conn_with_fetches([{"id": 1}, _SAMPLE_ROW])

        select_token(conn, AgentProvider.CLAUDE)

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

        claims = [select_token(conn, AgentProvider.CLAUDE).id for _ in range(10)]

        assert claims == [1, 2, 3, 1, 2, 3, 1, 2, 3, 1]
        assert {token_id: claims.count(token_id) for token_id in {1, 2, 3}} == {
            1: 4,
            2: 3,
            3: 3,
        }


class TestSetTokenPriority:
    def test_set_priority_updates_row(self):
        conn, cursor = _mock_conn(rowcount=1)

        result = set_token_priority(conn, 42, 5)

        assert result is True
        sql = cursor.execute.call_args[0][0]
        assert "UPDATE oauth_token SET priority" in sql
        params = cursor.execute.call_args[0][1]
        assert params == (5, 42)

    def test_set_priority_returns_false_when_token_not_found(self):
        conn, _cursor = _mock_conn(rowcount=0)

        result = set_token_priority(conn, 999, 0)

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
        if "SELECT id FROM oauth_token" in sql:
            agent_type = params[0]
            candidates = [
                row for row in self.conn.rows.values()
                if row["agent_type"] == agent_type
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
            self._result = {"id": self._selected_id} if self._selected_id else None
            return
        if "UPDATE oauth_token SET used_at = UTC_TIMESTAMP(6)" in sql:
            token_id = params[0]
            self.conn._tick += 1
            self.conn.rows[token_id]["used_at"] = datetime(
                2026, 1, 1, 0, 0, 0, self.conn._tick,
            )
            self._result = None
            return
        if "SELECT * FROM oauth_token WHERE id = %s" in sql:
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
        assert "UPDATE oauth_token" in sql
        assert "WHERE id = %s" in sql
        # Must never touch operator/health/identity columns (only credentials,
        # expires_at, updated_at). "oauth_token" contains none of these as substrings.
        for col in ("enabled", "status", "error_msg", "priority", "label", "type", "token_limit"):
            assert col not in sql
        assert params[-1] == 7                 # bound id (last param)
        assert params[0].startswith("aes256:")  # credentials encrypted, not plaintext

    def test_refreshes_expires_at_from_seconds_payload(self):
        conn, cursor = _mock_conn()
        update_refreshed_credentials(conn, 7, {"expires_at": 1799999999})
        _sql, params = cursor.execute.call_args[0]
        assert isinstance(params[1], datetime)   # parity with register_token

    def test_none_expires_at_writes_null(self):
        conn, cursor = _mock_conn()
        update_refreshed_credentials(conn, 7, {"expires_at": None})
        _sql, params = cursor.execute.call_args[0]
        assert params[1] is None

    def test_does_not_commit_caller_owns_transaction(self):
        conn, _ = _mock_conn()
        update_refreshed_credentials(conn, 7, {"x": 1})
        conn.commit.assert_not_called()
