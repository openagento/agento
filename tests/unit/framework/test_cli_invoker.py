"""Tests for CliInvoker protocol + registry."""
from __future__ import annotations

import pytest

from agento.framework.agent_manager.models import AgentProvider
from agento.framework.cli_invoker import (
    CliInvoker,
    clear,
    get_cli_invoker,
    register_cli_invoker,
)


class _FakeInvoker:
    def interactive_command(self):
        return ["fake"]

    def headless_command(self, prompt, *, model=None):
        cmd = ["fake", "-p", prompt]
        if model:
            cmd.extend(["--model", model])
        return cmd


class _BadInvoker:
    """Missing methods — fails the Protocol check."""


def setup_function():
    clear()


def test_protocol_accepts_duck_typed_invoker():
    assert isinstance(_FakeInvoker(), CliInvoker)


def test_protocol_rejects_class_missing_methods():
    assert not isinstance(_BadInvoker(), CliInvoker)


def test_register_and_get_by_enum():
    invoker = _FakeInvoker()
    register_cli_invoker(AgentProvider.CLAUDE, invoker)
    assert get_cli_invoker(AgentProvider.CLAUDE) is invoker


def test_register_and_get_by_string():
    invoker = _FakeInvoker()
    register_cli_invoker(AgentProvider.CLAUDE, invoker)
    assert get_cli_invoker("claude") is invoker


def test_get_unknown_provider_string_raises_value_error():
    with pytest.raises(ValueError):
        get_cli_invoker("no-such-provider")


def test_get_unregistered_provider_raises_key_error():
    with pytest.raises(KeyError, match="No CliInvoker registered"):
        get_cli_invoker(AgentProvider.CLAUDE)


def test_clear_removes_all_registrations():
    register_cli_invoker(AgentProvider.CLAUDE, _FakeInvoker())
    clear()
    with pytest.raises(KeyError):
        get_cli_invoker(AgentProvider.CLAUDE)


class TestClaudeCliInvokerImpl:
    def test_interactive_returns_binary(self):
        from agento.modules.claude.src.cli import ClaudeCliInvoker
        assert ClaudeCliInvoker().interactive_command() == ["claude"]

    def test_interactive_yolo_appends_bypass_flag(self):
        from agento.modules.claude.src.cli import ClaudeCliInvoker
        assert ClaudeCliInvoker().interactive_command(yolo=True) == [
            "claude", "--dangerously-skip-permissions",
        ]

    def test_headless_without_model(self):
        from agento.modules.claude.src.cli import ClaudeCliInvoker
        cmd = ClaudeCliInvoker().headless_command("hi")
        assert cmd == [
            "claude", "-p", "hi",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
        ]

    def test_headless_with_model(self):
        from agento.modules.claude.src.cli import ClaudeCliInvoker
        cmd = ClaudeCliInvoker().headless_command("hi", model="opus-4")
        assert cmd[-2:] == ["--model", "opus-4"]


class TestCodexCliInvokerImpl:
    def test_interactive_returns_binary(self):
        from agento.modules.codex.src.cli import CodexCliInvoker
        assert CodexCliInvoker().interactive_command() == ["codex"]

    def test_interactive_yolo_appends_bypass_flag(self):
        from agento.modules.codex.src.cli import CodexCliInvoker
        assert CodexCliInvoker().interactive_command(yolo=True) == [
            "codex", "--dangerously-bypass-approvals-and-sandbox",
        ]

    def test_headless_without_model(self):
        from agento.modules.codex.src.cli import CodexCliInvoker
        cmd = CodexCliInvoker().headless_command("hi")
        assert cmd == [
            "codex", "exec", "hi",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]

    def test_headless_with_model(self):
        from agento.modules.codex.src.cli import CodexCliInvoker
        cmd = CodexCliInvoker().headless_command("hi", model="gpt-5.4")
        assert cmd[-2:] == ["--model", "gpt-5.4"]
