"""Regression tests for impl review round 5.

The headline finding was a genuine data-integrity bug: credential labels were unique
GLOBALLY, so registering the same label under a second scope overwrote the first scope's
encrypted credentials and left the row pointing at the wrong scope.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.harness import RunRequest, clear
from tests.harness_fixtures import make_runner, register_builtin_harnesses

REPO = Path(__file__).resolve().parents[4]
SQL = REPO / "src" / "agento" / "framework" / "sql"

SENTINEL = "SECRET-PROMPT-CONTENT-b7f3"


class TestCredentialLabelsAreScopedNotGlobal:
    """`UNIQUE(label)` predated credential scopes. With it, `credential:register codex
    my-token` after `credential:register claude my-token` did not insert — the upsert
    matched on label alone, overwrote claude's encrypted credentials, and left `scope`
    unchanged (scope is not in the UPDATE list). One credential destroyed, and the survivor
    served Codex credentials under the Claude scope. The docs show exactly this label reuse.
    """

    def test_historical_plural_index_names_are_also_dropped(self):
        """Round 6: migration 005 named these with the PLURAL table ('oauth_tokens'), 013
        renamed only the table, and `migrate.py` swallows 1091 — so dropping only the
        singular spellings silently no-op'd on every upgraded database while still being
        recorded as applied. Behaviour is pinned by
        tests/integration/test_credential_index_upgrade_path.py, which runs the real chain.
        """
        cleanup = (SQL / "033_drop_historical_credential_indexes.sql").read_text()
        for name in (
            "uq_oauth_tokens_label",            # plural, from 005
            "uq_oauth_token_label",             # singular, from 032
            "idx_oauth_tokens_agent_enabled",   # plural, from 005
            "idx_oauth_token_agent_enabled",    # singular, from 030
        ):
            assert f"DROP INDEX {name}" in cleanup, name
        # And re-asserts the scoped key, since 032's ADD may have been the only part
        # that took effect.
        assert "ADD UNIQUE KEY uq_credential_scope_label (scope, label)" in cleanup

    def test_cleanup_migration_is_seeded_for_fresh_installs(self):
        init = (SQL / "init" / "000_init.sql").read_text()
        assert "('033_drop_historical_credential_indexes')" in init

    def test_migration_replaces_the_global_key_with_a_scoped_one(self):
        migration = (SQL / "032_credential_label_unique_per_scope.sql").read_text()
        assert "DROP INDEX uq_oauth_token_label" in migration
        assert re.search(
            r"ADD UNIQUE KEY uq_credential_scope_label \(scope, label\)", migration,
        )

    def test_fresh_install_schema_has_only_the_scoped_key(self):
        init = (SQL / "init" / "000_init.sql").read_text()
        block = init[init.index("CREATE TABLE IF NOT EXISTS credential"):]
        block = block[: block.index("ENGINE=InnoDB")]

        assert "uq_credential_scope_label (scope, label)" in block
        assert "uq_oauth_token_label (label)" not in block

    def test_migration_is_seeded_for_fresh_installs(self):
        init = (SQL / "init" / "000_init.sql").read_text()
        assert "('032_credential_label_unique_per_scope')" in init

    def test_post_upsert_lookup_is_keyed_on_scope_and_label(self):
        """Selecting by label alone would return the OTHER scope's row."""
        from agento.framework.agent_manager import credential_store

        source = Path(credential_store.__file__).read_text()
        assert "SELECT id FROM credential WHERE scope = %s AND label = %s" in source
        assert "SELECT id FROM credential WHERE label = %s" not in source

    def test_register_queries_with_both_columns(self):
        """Drive the real function and inspect the SQL it issues."""
        from agento.framework.agent_manager.credential_store import register_credential

        cursor = MagicMock()
        cursor.lastrowid = 0  # simulate the UPDATE branch of the upsert
        # First fetchone answers the id lookup; the second returns the full row.
        cursor.fetchone.side_effect = [
            {"id": 7},
            {
                "id": 7, "scope": "codex", "agent_type": "codex", "type": "oauth",
                "label": "my-token", "credentials": None, "token_limit": 0,
                "enabled": 1, "status": "ok", "error_msg": None,
                "priority": 0, "expires_at": None, "used_at": None,
                "created_at": None, "updated_at": None,
            },
        ]
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda _s: cursor
        conn.cursor.return_value.__exit__ = lambda *_a: False

        with patch(
            "agento.framework.agent_manager.credential_store.encrypt_credentials",
            return_value="aes256:x",
        ):
            register_credential(
                conn, scope="codex", label="my-token", credentials={"api_key": "k"},
            )

        select = next(
            c for c in cursor.execute.call_args_list
            if c.args[0].strip().startswith("SELECT id")
        )
        assert select.args[1] == ("codex", "my-token")


class TestPromptNeverEntersLogsAtAnyLevel:
    """Round 5 replaced the full-argv DEBUG log with a redacted one. Round 10 removed the
    argv from logs entirely (see TestArgvIsNeverLogged in the round-9 file) — a strictly
    stronger guarantee. These tests keep pinning the property, not the mechanism.
    """

    @pytest.fixture(autouse=True)
    def _harnesses(self):
        register_builtin_harnesses()
        yield
        clear()

    def _runner(self):
        runner = make_runner("claude", credential=None, credential_required=False)
        runner._record_usage = MagicMock()
        runner.logger = logging.getLogger("round5-log-test")
        runner._execute_process = MagicMock(
            return_value=MagicMock(
                returncode=0,
                stdout='{"type":"result","result":"ok","usage":{}}\n',
                stderr="",
            ),
        )
        return runner

    def test_prompt_absent_at_debug(self, caplog):
        runner = self._runner()
        with caplog.at_level(logging.DEBUG, logger="round5-log-test"):
            runner.execute(RunRequest(prompt=SENTINEL))

        assert SENTINEL not in caplog.text

    def test_metadata_is_logged_instead(self, caplog):
        runner = self._runner()
        with caplog.at_level(logging.INFO, logger="round5-log-test"):
            runner.execute(RunRequest(prompt=SENTINEL))

        assert "bin=claude" in caplog.text
        assert f"prompt_len={len(SENTINEL)}" in caplog.text

    def test_no_flags_are_logged_either(self, caplog):
        """Round 10: flag-level detail was the reason to keep the argv, but flags come from
        an untrusted builder and can carry values. Metadata only."""
        runner = self._runner()
        with caplog.at_level(logging.DEBUG, logger="round5-log-test"):
            runner.execute(RunRequest(prompt=SENTINEL))

        assert "--mcp-config" not in caplog.text

    def test_timeout_exception_carries_no_prompt(self):
        """TimeoutExpired.__str__ renders `cmd`, and that text is persisted to
        job.error_message where an operator (and any log shipper) reads it."""
        import subprocess

        runner = self._runner()
        runner.execute(RunRequest(prompt=SENTINEL))

        exc = subprocess.TimeoutExpired(
            cmd=runner._log_cmd, timeout=1, output="", stderr="",
        )
        assert SENTINEL not in str(exc)
        assert "prompt_len=" in str(exc)

    def test_the_timeout_path_uses_the_stored_metadata(self):
        from agento.framework.harness import subprocess_runner

        source = Path(subprocess_runner.__file__).read_text()
        assert 'cmd=self._log_cmd or (cmd[0] if cmd else "")' in source

    def test_no_call_site_logs_the_raw_command(self):
        from agento.framework.harness import subprocess_runner

        source = Path(subprocess_runner.__file__).read_text()
        assert "join(cmd)" not in source, "argv must never be rendered into a string"
        assert "self._log_cmd = self._cmd_metadata(cmd, request)" in source


class TestSetupRunsCrossModuleValidation:
    """The cross-module collision check existed but only in `validate_all()`, which
    `setup:upgrade` never calls — so a duplicate credential scope passed preflight and
    migrations began anyway."""

    def test_setup_calls_the_cross_module_check(self):
        from agento.framework import setup

        source = Path(setup.__file__).read_text()
        assert "cross_module_errors(enabled)" in source

    def test_collision_raises_before_any_migration(self, tmp_path):
        from agento.framework.setup import ModuleValidationError, _validate_manifests

        fixtures = REPO / "tests" / "fixtures" / "modules"
        claude = MagicMock()
        claude.name = "claude"
        claude.path = REPO / "src" / "agento" / "modules" / "claude"
        impostor = MagicMock()
        impostor.name = "scope_collision"
        impostor.path = fixtures / "scope_collision"

        with pytest.raises(ModuleValidationError) as exc:
            _validate_manifests([claude, impostor], logging.getLogger("t"))

        assert "manifest error" in str(exc.value)

    def test_clean_module_set_passes(self):
        from agento.framework.module_validator import cross_module_errors

        manifests = []
        for name in ("claude", "codex"):
            m = MagicMock()
            m.name = name
            m.path = REPO / "src" / "agento" / "modules" / name
            manifests.append(m)

        assert cross_module_errors(manifests) == []

    def test_check_operates_on_the_scanned_set_not_directories(self):
        """Taking manifests is what lets it cover extensions mounted outside core/user."""
        import inspect

        from agento.framework.module_validator import cross_module_errors

        params = list(inspect.signature(cross_module_errors).parameters)
        assert params == ["manifests"]
