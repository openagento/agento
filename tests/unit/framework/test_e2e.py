"""Tests for the e2e test harness (src.e2e) — mocked runner, no real LLM calls."""
from __future__ import annotations

from agento.framework.channels.base import PromptFragments
from agento.framework.channels.test import TestChannel
from agento.framework.e2e import _run_checks


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
