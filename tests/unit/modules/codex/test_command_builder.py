"""Codex CLI flags — in particular when the sandbox bypass flag must NOT be emitted."""
from __future__ import annotations

from agento.framework.harness import HarnessRunContext, RunRequest
from agento.modules.codex.src.command_builder import CodexCommandBuilder

BYPASS = "--dangerously-bypass-approvals-and-sandbox"


def _ctx(harness_config=None):
    return HarnessRunContext(
        harness="codex", provider="openai", model="gpt-5.4",
        harness_config=harness_config or {},
    )


def test_headless_keeps_bypass_by_default():
    cmd = CodexCommandBuilder().headless(_ctx(), RunRequest(prompt="hi"))
    assert BYPASS in cmd


def test_headless_drops_bypass_when_operator_sets_sandbox_mode():
    ctx = _ctx({"config": 'sandbox_mode = "workspace-write"\n'})
    cmd = CodexCommandBuilder().headless(ctx, RunRequest(prompt="hi"))
    assert BYPASS not in cmd
    assert "--skip-git-repo-check" in cmd and "--json" in cmd


def test_headless_keeps_bypass_when_operator_blob_is_unparseable():
    ctx = _ctx({"config": "sandbox_mode = "})
    cmd = CodexCommandBuilder().headless(ctx, RunRequest(prompt="hi"))
    assert BYPASS in cmd
