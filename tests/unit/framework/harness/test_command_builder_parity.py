"""Flag parity between the two ways a harness gets invoked.

Claude's flags used to live in TWO places — ``TokenClaudeRunner._build_command`` and
``ClaudeCliInvoker.headless_command`` — and had already drifted: the invoker omitted
``--mcp-config .mcp.json --strict-mcp-config``, so ``agento run <view> "<prompt>"``
started WITHOUT the per-job MCP config that the consumer path always injected (i.e. the
agent silently had no toolbox). One CommandBuilder per harness is what makes that
impossible; these tests pin it.
"""
from __future__ import annotations

import pytest

from agento.framework.harness import HarnessRunContext, RunRequest, get_harness

pytestmark = pytest.mark.usefixtures("builtin_harnesses")

_CLAUDE_MCP_FLAGS = ("--mcp-config", ".mcp.json", "--strict-mcp-config")


def _ctx(harness: str, **kwargs) -> HarnessRunContext:
    defaults = dict(harness=harness, provider="anthropic" if harness == "claude" else "openai")
    defaults.update(kwargs)
    return HarnessRunContext(**defaults)


def _builder(harness: str):
    return get_harness(harness).adapter.command_builder


class TestClaudeMcpFlagParity:
    def test_headless_carries_the_mcp_flags(self):
        cmd = _builder("claude").headless(_ctx("claude"), RunRequest(prompt="hi"))
        assert _contiguous(cmd, _CLAUDE_MCP_FLAGS)

    def test_interactive_carries_the_same_mcp_flags(self):
        """The regression this refactor fixes: interactive used to omit them."""
        cmd = _builder("claude").interactive(_ctx("claude"), yolo=False)
        assert _contiguous(cmd, _CLAUDE_MCP_FLAGS)

    def test_yolo_only_adds_the_bypass_flag(self):
        plain = _builder("claude").interactive(_ctx("claude"), yolo=False)
        yolo = _builder("claude").interactive(_ctx("claude"), yolo=True)
        assert set(yolo) - set(plain) == {"--dangerously-skip-permissions"}

    def test_resume_reuses_the_same_flag_set(self):
        fresh = _builder("claude").headless(_ctx("claude"), RunRequest(prompt="hi"))
        resumed = _builder("claude").headless(
            _ctx("claude"), RunRequest(prompt="", session_id="sess-1"),
        )
        assert _contiguous(resumed, _CLAUDE_MCP_FLAGS)
        assert "--resume" in resumed and "sess-1" in resumed
        # Everything after the prompt/resume preamble is identical.
        assert fresh[fresh.index("--dangerously-skip-permissions"):] == \
            resumed[resumed.index("--dangerously-skip-permissions"):]


class TestModelPlumbing:
    def test_request_model_wins_over_context_model(self):
        cmd = _builder("claude").headless(
            _ctx("claude", model="ctx-model"), RunRequest(prompt="hi", model="req-model"),
        )
        assert cmd[cmd.index("--model") + 1] == "req-model"

    def test_context_model_is_used_when_the_request_omits_one(self):
        cmd = _builder("claude").headless(
            _ctx("claude", model="ctx-model"), RunRequest(prompt="hi"),
        )
        assert cmd[cmd.index("--model") + 1] == "ctx-model"

    def test_no_model_means_no_flag(self):
        cmd = _builder("claude").headless(_ctx("claude"), RunRequest(prompt="hi"))
        assert "--model" not in cmd

    @pytest.mark.parametrize("harness", ["claude", "codex"])
    def test_interactive_model_flag_matches_headless(self, harness):
        interactive = _builder(harness).interactive(_ctx(harness, model="m1"))
        headless = _builder(harness).headless(_ctx(harness, model="m1"), RunRequest(prompt="p"))
        assert "m1" in interactive and "m1" in headless


class TestSecretsNeverInArgv:
    @pytest.mark.parametrize("harness", ["claude", "codex"])
    def test_no_credential_value_reaches_the_command(self, harness):
        """Secrets travel via env (name-only ``-e``) or on-disk auth, never argv, so they
        can't leak through ``ps`` or a CI log."""
        from tests.unit.agent_manager.conftest import make_token

        credential = make_token(
            id=1, label="l", credentials={"api_key": "sk-SECRET-VALUE"},
        )
        ctx = _ctx(harness, credential=credential, credential_required=True)

        for cmd in (
            _builder(harness).headless(ctx, RunRequest(prompt="hi")),
            _builder(harness).interactive(ctx),
        ):
            assert "sk-SECRET-VALUE" not in " ".join(cmd)

    @pytest.mark.parametrize("harness", ["claude", "codex"])
    def test_prompt_is_never_shell_interpolated(self, harness):
        """The prompt is an argv element, so shell metacharacters stay literal."""
        nasty = "'; rm -rf / #"
        cmd = _builder(harness).headless(_ctx(harness), RunRequest(prompt=nasty))
        assert nasty in cmd


def _contiguous(haystack: list[str], needle: tuple[str, ...]) -> bool:
    n = len(needle)
    return any(tuple(haystack[i:i + n]) == needle for i in range(len(haystack) - n + 1))
