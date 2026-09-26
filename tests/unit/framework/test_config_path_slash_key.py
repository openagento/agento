"""Tests for slash-containing field-name handling in config-path parsers.

Background: agent_view/system.json declares schema keys like
``"identity/ssh_private_key"``. The full DB path is 3-part
``agent_view/identity/ssh_private_key`` and used to fall through length-2 /
length-4 dispatch in is_path_obscure, config_set_auto_encrypt,
config_get_tree, scoped_config helpers, and CLI validators — leaving the
SSH key plaintext at rest and unmasked on display.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agento.framework.core_config import (
    _is_obscure_module_config,
    _parse_config_path,
    config_get,
    config_get_tree,
    config_list,
    config_set_auto_encrypt,
    is_path_obscure,
)
from agento.framework.scoped_config import Scope


@pytest.fixture()
def modules_dir(tmp_path, monkeypatch):
    """Synthetic core/user module dirs with slash-keyed obscure fields."""
    core_dir = tmp_path / "core_modules"
    user_dir = tmp_path / "user_modules"
    core_dir.mkdir()
    user_dir.mkdir()

    # agent_view-like module with slash-containing schema keys (system.json)
    av_dir = core_dir / "agent_view"
    av_dir.mkdir()
    (av_dir / "system.json").write_text(json.dumps({
        "identity/ssh_private_key": {
            "type": "obscure",
            "label": "SSH private key",
        },
        "identity/ssh_public_key": {
            "type": "textarea",
            "label": "SSH public key",
        },
        "scheduling/priority": {"type": "integer", "label": "Priority"},
        "policy/release_channel": {
            "type": "select",
            "label": "Release channel",
            "options": [
                {"value": "stable", "label": "Stable"},
                {"value": "beta", "label": "Beta"},
            ],
        },
    }))

    # Legacy-style module: slash-key declared inside module.json["config"]
    legacy_dir = user_dir / "legacy-mod"
    legacy_dir.mkdir()
    (legacy_dir / "module.json").write_text(json.dumps({
        "name": "legacy-mod",
        "config": {
            "auth/api_token": {"type": "obscure", "label": "API token"},
        },
    }))

    monkeypatch.setattr("agento.framework.bootstrap.CORE_MODULES_DIR", str(core_dir))
    monkeypatch.setattr("agento.framework.bootstrap.USER_MODULES_DIR", str(user_dir))
    return tmp_path


# ---------------------------------------------------------------------------
# _parse_config_path
# ---------------------------------------------------------------------------
class TestParseConfigPath:
    def test_two_part_module_field(self):
        assert _parse_config_path("jira/jira_token") == ("jira", None, "jira_token")

    def test_three_part_slash_key(self):
        assert _parse_config_path("agent_view/identity/ssh_private_key") == (
            "agent_view", None, "identity/ssh_private_key",
        )

    def test_four_part_tool_field(self):
        assert _parse_config_path("my_app/tools/mysql/pass") == (
            "my_app", "mysql", "pass",
        )

    def test_no_slash_returns_none(self):
        assert _parse_config_path("agent_view") is None

    def test_empty_returns_none(self):
        assert _parse_config_path("") is None

    def test_trailing_slash_returns_none(self):
        assert _parse_config_path("agent_view/") is None
        assert _parse_config_path("agent_view/identity/") is None

    def test_three_part_with_literal_tools_is_malformed(self):
        # "module/tools/x" is too short for tool form (needs 4 parts), and the
        # literal `tools` prefix means it's not a normal slash-key. Reject.
        assert _parse_config_path("my_app/tools/pass") is None

    def test_tools_legacy_is_slash_key(self):
        # Only exact `tools` triggers the tool form. `tools_legacy` is a slash-key.
        assert _parse_config_path("my_app/tools_legacy/foo") == (
            "my_app", None, "tools_legacy/foo",
        )

    def test_five_part_tool_field_rejected(self):
        # Tool form is strictly 4-part; deeper nesting under tools/ is malformed.
        assert _parse_config_path("my_app/tools/mysql/inner/field") is None


# ---------------------------------------------------------------------------
# is_path_obscure + auto-encrypt
# ---------------------------------------------------------------------------
class TestSchemaObscureForSlashKey:
    def test_is_path_obscure_three_part(self, modules_dir):
        assert is_path_obscure("agent_view/identity/ssh_private_key") is True

    def test_is_path_obscure_three_part_non_obscure(self, modules_dir):
        assert is_path_obscure("agent_view/identity/ssh_public_key") is False

    def test_is_path_obscure_unknown_slash_key(self, modules_dir):
        assert is_path_obscure("agent_view/identity/unknown") is False

    def test_is_obscure_module_config_accepts_slash_field(self, modules_dir):
        assert _is_obscure_module_config("agent_view", "identity/ssh_private_key") is True

    def test_legacy_module_json_slash_key(self, modules_dir):
        # module.json["config"] fallback path — same lookup, no new branch needed.
        assert is_path_obscure("legacy_mod/auth/api_token") is True


class TestAutoEncryptSlashKey:
    def test_encrypts_slash_key_obscure_field(self, modules_dir, monkeypatch):
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "test-key-slashkey")

        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        key_body = "-----BEGIN OPENSSH PRIVATE KEY-----\nDEADBEEF\n-----END OPENSSH PRIVATE KEY-----\n"
        encrypted = config_set_auto_encrypt(
            conn, "agent_view/identity/ssh_private_key", key_body,
        )

        assert encrypted is True, "slash-key obscure field must auto-encrypt"
        stored_value = cursor.execute.call_args[0][1][3]
        assert stored_value.startswith("aes256s:")
        assert "OPENSSH" not in stored_value

    def test_does_not_encrypt_non_obscure_slash_key(self, modules_dir):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        encrypted = config_set_auto_encrypt(
            conn, "agent_view/identity/ssh_public_key", "ssh-rsa AAAAB3...",
        )
        assert encrypted is False


# ---------------------------------------------------------------------------
# Read masking — exact, tree, list
# ---------------------------------------------------------------------------
def _mock_select_conn(rows: list[dict]):
    """Return a MagicMock conn whose first SELECT returns the given rows."""
    cursor = MagicMock()
    cursor.fetchall = MagicMock(return_value=rows)
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


class TestReadMaskingForPlaintextObscureRow:
    """Pre-fix DB state: encrypted=0 but path is schema-obscure. Must mask anyway."""

    PATH = "agent_view/identity/ssh_private_key"
    LEAKED = "-----BEGIN OPENSSH PRIVATE KEY-----LEAK"

    def test_config_get_marks_obscure(self, modules_dir):
        conn = _mock_select_conn([
            {"scope": "default", "scope_id": 0, "value": self.LEAKED, "encrypted": 0},
        ])
        rows = config_get(conn, self.PATH)
        assert rows[0]["obscure"] is True

    def test_config_get_tree_masks(self, modules_dir):
        conn = _mock_select_conn([
            {"scope": "default", "scope_id": 0, "path": self.PATH,
             "value": self.LEAKED, "encrypted": 0},
        ])
        rows = config_get_tree(conn, "agent_view/")
        assert len(rows) == 1
        assert rows[0]["value"] == "****"
        assert self.LEAKED not in rows[0]["value"]

    def test_config_list_masks(self, modules_dir):
        conn = _mock_select_conn([
            {"scope": "default", "scope_id": 0, "path": self.PATH,
             "value": self.LEAKED, "encrypted": 0},
        ])
        rows = config_list(conn, prefix="agent_view")
        assert rows[0]["value"] == "****"
        assert rows[0]["obscure"] is True


# ---------------------------------------------------------------------------
# CLI validators
# ---------------------------------------------------------------------------
class TestCliValidators:
    def test_validate_config_path_accepts_three_part(self, modules_dir):
        from agento.framework.cli.config import _validate_config_path

        assert _validate_config_path(
            "agent_view/identity/ssh_private_key", Scope.DEFAULT,
        ) is True

    def test_validate_config_path_rejects_unknown_slash_key(self, modules_dir, capsys):
        from agento.framework.cli.config import _validate_config_path

        ok = _validate_config_path("agent_view/identity/no_such_field", Scope.DEFAULT)
        assert ok is False
        captured = capsys.readouterr()
        assert "not found" in captured.out.lower()

    def test_validate_config_value_select_with_slash_key(self, modules_dir, capsys):
        from agento.framework.cli.config import _validate_config_value

        # Valid option passes
        assert _validate_config_value(
            "agent_view/policy/release_channel", "stable",
        ) is True
        # Invalid option is rejected
        assert _validate_config_value(
            "agent_view/policy/release_channel", "gamma",
        ) is False
        captured = capsys.readouterr()
        assert "release_channel" in captured.out


# ---------------------------------------------------------------------------
# Scoped resolver + env-var key
# ---------------------------------------------------------------------------
class TestEnvKeyNormalization:
    def test_env_key_for_slash_field(self):
        """Slash-containing field name maps to a valid env var name (slashes -> __)."""
        from agento.framework.config_resolver import path_to_env_key

        env_key = path_to_env_key("agent_view/identity/ssh_private_key")
        assert env_key == "CONFIG__AGENT_VIEW__IDENTITY__SSH_PRIVATE_KEY"
        assert "/" not in env_key

    def test_env_key_for_two_part_path(self):
        from agento.framework.config_resolver import path_to_env_key

        env_key = path_to_env_key("jira/jira_token")
        assert env_key == "CONFIG__JIRA__JIRA_TOKEN"

    def test_env_key_for_tool_path(self):
        from agento.framework.config_resolver import path_to_env_key

        env_key = path_to_env_key("my_app/tools/mysql/pass")
        assert env_key == "CONFIG__MY_APP__TOOLS__MYSQL__PASS"


class TestResolveConfigJsonForSlashKey:
    def test_default_lookup_finds_slash_key(self, modules_dir, monkeypatch):
        from dataclasses import dataclass

        from agento.framework.config_resolver import ScopedConfigService

        # Add a config.json for agent_view with a slash-key default
        av_dir = modules_dir / "core_modules" / "agent_view"
        (av_dir / "config.json").write_text(
            json.dumps({"identity/ssh_public_key": "ssh-rsa DEFAULT"}),
        )

        @dataclass
        class _M:
            name: str
            path: object

        monkeypatch.setattr(
            "agento.framework.bootstrap.get_manifests",
            lambda: [_M(name="agent_view", path=av_dir)],
        )

        svc = ScopedConfigService.__new__(ScopedConfigService)  # bypass __init__ (no DB)
        result = svc._resolve_config_json("agent_view/identity/ssh_public_key")
        assert result == "ssh-rsa DEFAULT"


# ---------------------------------------------------------------------------
# Data patch: re-encrypt rows that should be obscure but were stored plaintext
# ---------------------------------------------------------------------------
class TestEncryptObscureRowsAtRest:
    """The remediation patch installed by agent_view/data_patch.json."""

    def _import_patch(self):
        from agento.modules.agent_view.src.patches.encrypt_obscure_rows_at_rest import (
            EncryptObscureRowsAtRest,
        )
        return EncryptObscureRowsAtRest

    def _make_store_conn(self, initial_rows):
        store = {(r["scope"], r["scope_id"], r["path"]): (r["value"], int(r["encrypted"]))
                 for r in initial_rows}

        cursor = MagicMock()

        def execute(sql, params=None):
            up = sql.upper().lstrip()
            if up.startswith("SELECT"):
                cursor._rows = [
                    {"scope": s, "scope_id": sid, "path": p, "value": v, "encrypted": e}
                    for (s, sid, p), (v, e) in store.items()
                    # Honour `WHERE encrypted = 0` filter on the patch's SELECT;
                    # other SELECTs (none today) get the unfiltered store.
                    if "ENCRYPTED = 0" not in sql.upper() or e == 0
                ]
            elif up.startswith("UPDATE"):
                value, scope, scope_id, path = params
                store[(scope, scope_id, path)] = (value, 1)
                cursor._last_update = (scope, scope_id, path, value)

        def executemany(sql, seq_of_params):
            for params in seq_of_params:
                execute(sql, params)

        cursor.execute = execute
        cursor.executemany = executemany
        cursor.fetchall = lambda: getattr(cursor, "_rows", [])

        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        conn._store = store
        return conn

    def test_encrypts_plaintext_obscure_row(self, modules_dir, monkeypatch):
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "patch-test-key")
        Patch = self._import_patch()

        conn = self._make_store_conn([
            {"scope": "default", "scope_id": 0,
             "path": "agent_view/identity/ssh_private_key",
             "value": "PLAINTEXT-KEY", "encrypted": 0},
        ])
        Patch().apply(conn)

        value, enc = conn._store[("default", 0, "agent_view/identity/ssh_private_key")]
        assert enc == 1
        assert value.startswith("aes256s:")
        # Decrypt round-trip
        from agento.framework.encryptor import get_encryptor
        assert get_encryptor().decrypt(value) == "PLAINTEXT-KEY"

    def test_idempotent(self, modules_dir, monkeypatch):
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "patch-test-key")
        Patch = self._import_patch()

        conn = self._make_store_conn([
            {"scope": "default", "scope_id": 0,
             "path": "agent_view/identity/ssh_private_key",
             "value": "PLAINTEXT-KEY", "encrypted": 0},
        ])
        Patch().apply(conn)
        value_after_first, _ = conn._store[("default", 0, "agent_view/identity/ssh_private_key")]
        Patch().apply(conn)  # second run finds no encrypted=0 rows -> no-op
        value_after_second, enc = conn._store[("default", 0, "agent_view/identity/ssh_private_key")]
        assert enc == 1
        assert value_after_first == value_after_second

    def test_skips_non_obscure_plaintext_row(self, modules_dir, monkeypatch):
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "patch-test-key")
        Patch = self._import_patch()

        conn = self._make_store_conn([
            {"scope": "default", "scope_id": 0,
             "path": "agent_view/identity/ssh_public_key",
             "value": "ssh-rsa PUBLIC", "encrypted": 0},
        ])
        Patch().apply(conn)
        value, enc = conn._store[("default", 0, "agent_view/identity/ssh_public_key")]
        assert enc == 0
        assert value == "ssh-rsa PUBLIC"

    def test_requires_rename_patch(self):
        """Must declare dependency on RenameAgentConfigPrefix so the encryption
        patch always sees the final agent_view/* path shape when applied on a
        fresh install where both patches are pending."""
        Patch = self._import_patch()
        assert Patch().require() == ["agent_view/RenameAgentConfigPrefix"]
