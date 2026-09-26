"""Integration test: config:set auto-encrypts fields declared obscure in system.json.

Tests the full write→read cycle: config_set_auto_encrypt writes encrypted,
config_get reads back with encrypted flag, is_path_obscure detects schema type.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agento.framework.core_config import (
    _is_obscure_field,
    _is_obscure_module_config,
    config_get,
    config_set_auto_encrypt,
    is_path_obscure,
)


@pytest.fixture()
def modules_dir(tmp_path, monkeypatch):
    """Create temporary core/user module directories with jira/system.json."""
    core_dir = tmp_path / "core_modules"
    user_dir = tmp_path / "user_modules"
    core_dir.mkdir()
    user_dir.mkdir()

    # jira module (core) with system.json declaring jira_token as obscure
    jira_dir = core_dir / "jira"
    jira_dir.mkdir()
    (jira_dir / "system.json").write_text(json.dumps({
        "jira_host": {"type": "string", "label": "Jira host URL"},
        "jira_user": {"type": "string", "label": "Jira user email"},
        "jira_token": {"type": "obscure", "label": "Jira API token"},
    }))

    # example user module with module.json declaring tool field as obscure
    example_dir = user_dir / "my-app"
    example_dir.mkdir()
    (example_dir / "module.json").write_text(json.dumps({
        "name": "my-app",
        "tools": [{
            "type": "mysql",
            "name": "mysql_myapp_prod",
            "fields": {
                "host": {"type": "string", "label": "Host"},
                "pass": {"type": "obscure", "label": "Password"},
            },
        }],
    }))

    monkeypatch.setattr("agento.framework.bootstrap.CORE_MODULES_DIR", str(core_dir))
    monkeypatch.setattr("agento.framework.bootstrap.USER_MODULES_DIR", str(user_dir))
    return tmp_path


class TestIsObscureModuleConfig:
    def test_jira_token_is_obscure(self, modules_dir):
        assert _is_obscure_module_config("jira", "jira_token") is True

    def test_jira_host_is_not_obscure(self, modules_dir):
        assert _is_obscure_module_config("jira", "jira_host") is False

    def test_unknown_module_returns_false(self, modules_dir):
        assert _is_obscure_module_config("nonexistent", "token") is False

    def test_unknown_field_returns_false(self, modules_dir):
        assert _is_obscure_module_config("jira", "nonexistent_field") is False


class TestIsObscureField:
    def test_tool_pass_is_obscure(self, modules_dir):
        assert _is_obscure_field("my-app", "mysql_myapp_prod", "pass") is True

    def test_tool_host_is_not_obscure(self, modules_dir):
        assert _is_obscure_field("my-app", "mysql_myapp_prod", "host") is False

    def test_hyphen_underscore_lookup(self, modules_dir):
        # module dir is "my-app" but path uses "my_app"
        assert _is_obscure_field("my_app", "mysql_myapp_prod", "pass") is True


class TestIsPathObscure:
    def test_module_config_path(self, modules_dir):
        assert is_path_obscure("jira/jira_token") is True
        assert is_path_obscure("jira/jira_host") is False

    def test_tool_config_path(self, modules_dir):
        assert is_path_obscure("my_app/tools/mysql_myapp_prod/pass") is True
        assert is_path_obscure("my_app/tools/mysql_myapp_prod/host") is False


class TestConfigSetAutoEncrypt:
    def test_encrypts_obscure_module_field(self, modules_dir, monkeypatch):
        """THE BUG: config:set jira/jira_token should auto-encrypt because system.json says obscure."""
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "test-key-for-autoencrypt")

        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        encrypted = config_set_auto_encrypt(conn, "jira/jira_token", "my-secret-token")

        assert encrypted is True, "jira/jira_token should be auto-encrypted (type: obscure in system.json)"
        # Verify the stored value is aes256-encrypted, not plaintext
        args = cursor.execute.call_args[0]
        stored_value = args[1][3]  # 4th param in INSERT
        assert stored_value.startswith("aes256s:"), f"Expected aes256s prefix, got: {stored_value[:20]}"

    def test_does_not_encrypt_non_obscure_field(self, modules_dir, monkeypatch):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        encrypted = config_set_auto_encrypt(conn, "jira/jira_host", "https://jira.test")

        assert encrypted is False
        args = cursor.execute.call_args[0]
        stored_value = args[1][3]
        assert stored_value == "https://jira.test"

    def test_encrypts_obscure_tool_field(self, modules_dir, monkeypatch):
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "test-key-for-tool")

        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        encrypted = config_set_auto_encrypt(conn, "my_app/tools/mysql_myapp_prod/pass", "secret123")

        assert encrypted is True
        args = cursor.execute.call_args[0]
        stored_value = args[1][3]
        assert stored_value.startswith("aes256s:")


class TestWriteReadRoundtrip:
    """Full cycle: config_set_auto_encrypt → DB → config_get → display masking."""

    def _make_in_memory_db(self):
        """Simulate core_config_data with a dict-backed mock connection."""
        store = {}  # {(scope, scope_id, path): (value, encrypted)}

        def make_cursor():
            cur = MagicMock()
            cur._last_sql = None
            cur._last_params = None

            def execute(sql, params=None):
                cur._last_sql = sql
                cur._last_params = params
                if "INSERT" in sql:
                    scope, scope_id, path, value, enc = params
                    store[(scope, scope_id, path)] = (value, int(enc))
                elif "SELECT" in sql and "WHERE path" in sql:
                    path = params[0]
                    cur._rows = [
                        {"scope": s, "scope_id": sid, "value": v, "encrypted": enc}
                        for (s, sid, p), (v, enc) in store.items()
                        if p == path
                    ]

            def fetchall():
                return getattr(cur, "_rows", [])

            cur.execute = execute
            cur.fetchall = fetchall
            return cur

        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(side_effect=make_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn, store

    def test_obscure_field_encrypted_in_db_and_masked_on_read(self, modules_dir, monkeypatch):
        """Write jira/jira_token → stored encrypted → config_get returns encrypted=True."""
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "roundtrip-test-key")
        conn, store = self._make_in_memory_db()

        # Write (simulates bin/agento config:set jira/jira_token <secret>)
        encrypted = config_set_auto_encrypt(conn, "jira/jira_token", "my-secret-token")
        assert encrypted is True

        # Verify DB contains encrypted value, not plaintext
        db_key = ("default", 0, "jira/jira_token")
        assert db_key in store
        stored_value, stored_enc = store[db_key]
        assert stored_enc == 1
        assert stored_value.startswith("aes256s:")
        assert "my-secret-token" not in stored_value

        # Read back (simulates bin/agento config:get jira/jira_token)
        rows = config_get(conn, "jira/jira_token")
        assert len(rows) == 1
        assert rows[0]["encrypted"] is True
        assert rows[0]["obscure"] is True

    def test_non_obscure_field_stored_plaintext(self, modules_dir, monkeypatch):
        """Write jira/jira_host → stored plaintext → config_get returns encrypted=False."""
        conn, store = self._make_in_memory_db()

        encrypted = config_set_auto_encrypt(conn, "jira/jira_host", "https://jira.test")
        assert encrypted is False

        db_key = ("default", 0, "jira/jira_host")
        stored_value, stored_enc = store[db_key]
        assert stored_enc == 0
        assert stored_value == "https://jira.test"

        rows = config_get(conn, "jira/jira_host")
        assert len(rows) == 1
        assert rows[0]["encrypted"] is False
        assert rows[0]["obscure"] is False
        assert rows[0]["value"] == "https://jira.test"

    def test_scoped_obscure_write_read(self, modules_dir, monkeypatch):
        """Write jira/jira_token with scope=agent_view → encrypted in scoped row."""
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "scoped-test-key")
        conn, store = self._make_in_memory_db()

        encrypted = config_set_auto_encrypt(
            conn, "jira/jira_token", "scoped-secret",
            scope="agent_view", scope_id=5,
        )
        assert encrypted is True

        db_key = ("agent_view", 5, "jira/jira_token")
        assert db_key in store
        stored_value, _ = store[db_key]
        assert stored_value.startswith("aes256s:")
