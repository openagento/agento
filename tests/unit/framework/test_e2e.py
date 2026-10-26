"""Tests for the e2e test harness (src.e2e) — mocked runner, no real LLM calls."""
from __future__ import annotations

from agento.framework.channels.base import PromptFragments
from agento.framework.channels.test import TestChannel
from agento.framework.e2e import _run_checks

_OK_ROW = {
    "status": "SUCCESS", "agent_type": "claude", "model": "haiku", "input_tokens": 10,
    "output_tokens": 5, "prompt": "p", "output": "o", "result_summary": "session_id=s",
}


class TestTestChannel:
    def test_name(self):
        ch = TestChannel()
        assert ch.name == "blank"

    def test_get_prompt_fragments(self):
        ch = TestChannel()
        f = ch.get_prompt_fragments("E2E-1")
        assert isinstance(f, PromptFragments)
        assert "E2E-1" in f.read_context
        assert "OK" in f.respond

    def test_get_followup_fragments(self):
        ch = TestChannel()
        f = ch.get_followup_fragments("E2E-1", "do something")
        assert isinstance(f, PromptFragments)
        assert "E2E-1" in f.read_context


class TestRunChecks:
    def test_all_pass(self):
        row = {
            "status": "SUCCESS",
            "agent_type": "claude",
            "model": "claude-sonnet-4-20250514",
            "input_tokens": 150,
            "output_tokens": 10,
            "prompt": "test prompt",
            "output": "OK",
            "result_summary": "session_id=success turns=1 in=150 out=10",
        }
        checks = _run_checks(row)
        assert all(ok for _, ok, _ in checks)

    def test_failure_on_dead_status(self):
        row = {
            "status": "DEAD",
            "agent_type": "claude",
            "model": "claude-sonnet-4-20250514",
            "input_tokens": 0,
            "output_tokens": None,
            "prompt": "test",
            "output": None,
            "result_summary": None,
        }
        checks = _run_checks(row)
        labels_failed = [label for label, ok, _ in checks if not ok]
        assert "status=SUCCESS" in labels_failed
        assert "output saved" in labels_failed

    def test_missing_model(self):
        row = {
            "status": "SUCCESS",
            "agent_type": "codex",
            "model": None,
            "input_tokens": 100,
            "output_tokens": None,
            "prompt": "test",
            "output": "OK",
            "result_summary": "session_id=ok in=100",
        }
        checks = _run_checks(row)
        failed = {label for label, ok, _ in checks if not ok}
        assert "model set" in failed

    def test_toolbox_telemetry_checked_only_for_an_agent_view_job(self):
        row = {**_OK_ROW, "toolbox_mcp_connected": None, "toolbox_mcp_calls": None}
        assert all(ok for _, ok, _ in _run_checks(row))
        failed = {label for label, ok, _ in _run_checks(row, agent_view=True) if not ok}
        assert failed == {"toolbox_mcp_connected = 1", "toolbox_mcp_calls counted"}

    def test_connected_not_required_from_a_harness_without_an_init_report(self):
        row = {**_OK_ROW, "toolbox_mcp_connected": None, "toolbox_mcp_calls": 0}
        assert all(ok for _, ok, _ in _run_checks(row, agent_view=True, mcp_init_report=False))

    def test_toolbox_telemetry_passes_with_zero_calls(self):
        row = {**_OK_ROW, "toolbox_mcp_connected": 1, "toolbox_mcp_calls": 0}
        assert all(ok for _, ok, _ in _run_checks(row, agent_view=True))


class TestInsertTestJob:
    def test_agent_view_id_reaches_the_job_row(self, monkeypatch):
        from unittest.mock import MagicMock

        from agento.framework import e2e

        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.lastrowid = 7
        monkeypatch.setattr(e2e, "get_connection", lambda _cfg: conn)

        assert e2e._insert_test_job(MagicMock(), "REF", 42) == 7
        sql, params = cur.execute.call_args.args
        assert "agent_view_id" in sql
        assert "'2038-01-01 00:00:00'" in sql  # parked: a live consumer never claims it
        assert params[:2] == ("REF", 42)
        assert params[-2:] == (None, None)  # no pin: the consumer resolves from config

    def test_harness_and_model_pin_the_job_row(self, monkeypatch):
        from unittest.mock import MagicMock

        from agento.framework import e2e

        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.lastrowid = 7
        monkeypatch.setattr(e2e, "get_connection", lambda _cfg: conn)

        e2e._insert_test_job(MagicMock(), "REF", 42, harness="codex", model="gpt-x")
        sql, params = cur.execute.call_args.args
        assert "agent_type, model" in sql
        assert params[-2:] == ("codex", "gpt-x")


class TestPickCredential:
    """``agento e2e`` tests the credential the consumer will select for the job."""

    @staticmethod
    def _patch(monkeypatch, *, harness="codex", required=True, by_id=None, pool=None):
        from types import SimpleNamespace

        from agento.framework import agent_view_runtime, e2e
        from agento.framework import harness as harness_pkg
        from agento.framework.agent_view_runtime import AgentViewRuntime

        monkeypatch.setattr(
            agent_view_runtime, "resolve_agent_view_runtime",
            lambda _c, _v, **_k: AgentViewRuntime(harness=harness, provider="p"),
        )
        monkeypatch.setattr(
            harness_pkg, "find_harness",
            lambda h: SimpleNamespace(descriptor=SimpleNamespace(default_provider=f"{h}-default")),
        )
        resolved = []
        monkeypatch.setattr(
            harness_pkg, "resolve_provider",
            lambda h, p: resolved.append((h, p)) or SimpleNamespace(
                credential_required=required, credential_scope=h,
            ),
        )
        monkeypatch.setattr(e2e, "get_credential", lambda _c, _i: by_id)
        selected = []
        monkeypatch.setattr(e2e, "select_credential", lambda _c, s: selected.append(s) or pool)
        return selected, resolved

    @staticmethod
    def _cred(scope):
        from agento.framework.agent_manager.models import CredentialRecord

        return CredentialRecord(id=1, scope=scope, type="oauth", label="l")

    def test_selects_from_the_job_scope_only(self, monkeypatch):
        from agento.framework.e2e import _pick_credential

        selected, _ = self._patch(monkeypatch, pool=self._cred("codex"))
        assert _pick_credential(None, 5, None).scope == "codex"
        assert selected == ["codex"]

    def test_a_pinned_harness_selects_from_its_own_pool(self, monkeypatch):
        """The view runs codex; a job pinned to claude takes claude's default provider
        and claude's pool — exactly what the consumer resolves at claim."""
        from agento.framework.e2e import _pick_credential

        selected, resolved = self._patch(monkeypatch, pool=self._cred("claude"))
        assert _pick_credential(None, 5, None, "claude").scope == "claude"
        assert resolved == [("claude", "claude-default")]
        assert selected == ["claude"]

    def test_explicit_credential_of_another_scope_is_rejected(self, monkeypatch):
        import pytest

        from agento.framework.e2e import _pick_credential

        self._patch(monkeypatch, by_id=self._cred("claude"))
        with pytest.raises(ValueError, match="scope 'claude'"):
            _pick_credential(None, 5, 1)

    def test_explicit_credential_of_the_job_scope_is_used(self, monkeypatch):
        from agento.framework.e2e import _pick_credential

        self._patch(monkeypatch, by_id=self._cred("codex"))
        assert _pick_credential(None, 5, 1).scope == "codex"

    def test_empty_pool_unset_harness_and_no_credential_needed_raise(self, monkeypatch):
        import pytest

        from agento.framework.e2e import _pick_credential

        self._patch(monkeypatch, pool=None)
        with pytest.raises(ValueError, match="No healthy credential in scope 'codex'"):
            _pick_credential(None, None, None)
        self._patch(monkeypatch, harness=None)
        with pytest.raises(ValueError, match="harness"):
            _pick_credential(None, None, None)
        self._patch(monkeypatch, required=False)
        with pytest.raises(ValueError, match="needs no credential"):
            _pick_credential(None, None, None)


def test_the_scenario_claims_only_the_credential_under_test(monkeypatch):
    """Pinned on the consumer, not by disabling the scope's other credentials — a run
    in parallel, or the stack's own consumer, keeps the whole pool."""
    from unittest.mock import MagicMock

    from agento.framework import e2e
    from agento.framework.agent_manager.models import CredentialRecord

    consumer_cls = MagicMock()
    consumer_cls.return_value._try_dequeue.return_value = None
    monkeypatch.setattr(e2e, "Consumer", consumer_cls)
    monkeypatch.setattr(e2e, "_insert_test_job", lambda *a, **k: 1)
    monkeypatch.setattr(e2e, "get_connection", MagicMock(side_effect=AssertionError("no DB write")))

    cred = CredentialRecord(id=9, scope="claude", type="oauth", label="l")
    assert e2e.run_scenario(cred, MagicMock(), MagicMock(), MagicMock()) is False
    assert consumer_cls.call_args.kwargs["credential_id"] == 9
