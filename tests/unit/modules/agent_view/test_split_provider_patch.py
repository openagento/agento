"""The data patch that splits the pre-0.15 ``agent_view/provider`` into harness+provider.

A data patch is applied ONCE and tracked permanently in ``data_patch``, so it must not
depend on which modules happen to be enabled at upgrade time. Reading the harness
registry here would skip ``provider=codex`` rows on a deployment with the codex module
disabled, and a later ``module:enable codex`` would never re-run the patch — leaving that
config permanently unmigrated. Hence the FROZEN historical map.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agento.modules.agent_view.src.patches.split_provider_into_harness import (
    _HISTORICAL_HARNESS_PROVIDERS,
    SplitProviderIntoHarness,
)


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _conn(rows):
    cursor = _Cursor(rows)
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def _writes(cursor, kind: str) -> list[tuple[str, tuple | None]]:
    return [c for c in cursor.executed if c[0].startswith(kind)]


class TestSplitProviderIntoHarness:
    def test_migrates_a_legacy_row_into_both_paths(self):
        conn, cursor = _conn([{"scope": "agent_view", "scope_id": 7, "value": "codex"}])

        SplitProviderIntoHarness().apply(conn)

        (insert,) = _writes(cursor, "INSERT")
        assert "agent_view/harness" in insert[0]
        assert insert[1] == ("agent_view", 7, "codex")

        (update,) = _writes(cursor, "UPDATE")
        assert update[1] == ("openai", "agent_view", 7)
        conn.commit.assert_called_once()

    def test_migrates_codex_rows_even_when_the_module_is_disabled(self, monkeypatch):
        """The reason the map is frozen: a registry lookup would skip this row forever,
        because the patch is recorded as applied and never re-runs."""
        from agento.framework.harness import clear

        clear()  # no harnesses registered at all — the worst case
        conn, cursor = _conn([{"scope": "default", "scope_id": 0, "value": "codex"}])

        SplitProviderIntoHarness().apply(conn)

        assert _writes(cursor, "UPDATE")[0][1] == ("openai", "default", 0)

    def test_already_migrated_value_is_left_alone(self):
        """Idempotent: a second pass sees ``anthropic``, which is not a pre-0.15 harness
        value, and touches nothing."""
        conn, cursor = _conn([{"scope": "default", "scope_id": 0, "value": "anthropic"}])

        SplitProviderIntoHarness().apply(conn)

        assert _writes(cursor, "INSERT") == []
        assert _writes(cursor, "UPDATE") == []

    def test_unknown_value_is_left_alone_rather_than_guessed(self):
        conn, cursor = _conn([{"scope": "default", "scope_id": 0, "value": "hermes"}])

        SplitProviderIntoHarness().apply(conn)

        assert _writes(cursor, "INSERT") == []
        assert _writes(cursor, "UPDATE") == []

    def test_insert_ignore_never_overwrites_a_hand_set_harness(self):
        """Someone may have configured the new key by hand between upgrade and patch."""
        conn, cursor = _conn([{"scope": "agent_view", "scope_id": 1, "value": "claude"}])

        SplitProviderIntoHarness().apply(conn)

        assert _writes(cursor, "INSERT")[0][0].startswith("INSERT IGNORE")

    def test_tuple_rows_are_handled_like_dict_rows(self):
        conn, cursor = _conn([("workspace", 2, "claude")])

        SplitProviderIntoHarness().apply(conn)

        assert _writes(cursor, "INSERT")[0][1] == ("workspace", 2, "claude")
        assert _writes(cursor, "UPDATE")[0][1] == ("anthropic", "workspace", 2)

    def test_no_rows_is_a_noop_that_still_commits(self):
        conn, cursor = _conn([])

        SplitProviderIntoHarness().apply(conn)

        assert _writes(cursor, "INSERT") == []
        conn.commit.assert_called_once()

    def test_runs_after_the_prefix_rename_patch(self):
        assert SplitProviderIntoHarness().require() == ["RenameAgentConfigPrefix"]

    def test_map_covers_only_historical_values(self):
        """New harnesses must NOT be added here — they never had a pre-0.15 config."""
        assert _HISTORICAL_HARNESS_PROVIDERS == {
            "claude": "anthropic", "codex": "openai",
        }

    @pytest.mark.parametrize("harness,provider", sorted(_HISTORICAL_HARNESS_PROVIDERS.items()))
    def test_each_mapped_pair_is_actually_valid(self, harness, provider):
        """The frozen map must agree with what the shipped modules declare, or the patch
        would write a pair the runtime then rejects."""
        from agento.framework.harness import clear, resolve_provider
        from tests.harness_fixtures import register_builtin_harnesses

        register_builtin_harnesses()
        try:
            assert resolve_provider(harness, provider).id == provider
        finally:
            clear()
