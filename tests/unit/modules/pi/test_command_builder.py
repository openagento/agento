"""Pi command construction — flags, the resume contract, and the stdin channel."""
from __future__ import annotations

import pytest

from agento.framework.harness import HarnessRunContext, RunRequest
from agento.modules.pi.src.command_builder import RESUME_PROMPT, PiCommandBuilder


def ctx(**over) -> HarnessRunContext:
    base = dict(harness="pi", provider="openrouter", model="anthropic/claude-sonnet-4.5")
    base.update(over)
    return HarnessRunContext(**base)


@pytest.fixture
def builder():
    return PiCommandBuilder()


class TestHeadless:
    def test_carries_json_mode_offline_and_the_bridge(self, builder):
        cmd = builder.headless(ctx(), RunRequest(prompt="hi"))
        assert cmd[:2] == ["pi", "--mode"]
        assert "json" in cmd
        assert "--offline" in cmd
        assert cmd[cmd.index("-e") + 1] == ".pi/agento-toolbox.js"
        assert cmd[cmd.index("--model") + 1] == "anthropic/claude-sonnet-4.5"

    def test_the_prompt_is_never_in_argv(self, builder):
        """`-p` rejects a value starting with `-`, and Agento prompts come from Jira
        titles and mail subjects — so the prompt goes on stdin, always."""
        cmd = builder.headless(ctx(), RunRequest(prompt="-- review the PR"))
        assert "-- review the PR" not in cmd
        assert "-p" not in cmd

    def test_a_missing_model_fails_with_a_useful_message(self, builder):
        with pytest.raises(ValueError, match="requires a model"):
            builder.headless(ctx(model=None), RunRequest(prompt="hi"))

    def test_request_model_overrides_the_context(self, builder):
        cmd = builder.headless(ctx(), RunRequest(prompt="hi", model="other/model"))
        assert cmd[cmd.index("--model") + 1] == "other/model"


class TestResumeFlags:
    def test_session_id_only_when_resuming(self, builder):
        assert "--session-id" not in builder.headless(ctx(), RunRequest(prompt="hi"))
        cmd = builder.headless(ctx(), RunRequest(prompt="", session_id="S1"))
        assert cmd[cmd.index("--session-id") + 1] == "S1"

    @pytest.mark.parametrize("flag", ["--session", "--continue", "--resume"])
    def test_the_dangerous_resume_flags_are_never_used(self, builder, flag):
        """`--session <bare id>` in another cwd bucket makes Pi ask "Fork this session?"
        ON STDIN, which headless would answer with the prompt. All three are also
        mutually exclusive with `--session-id` (hard exit 1)."""
        for cmd in (
            builder.headless(ctx(), RunRequest(prompt="hi")),
            builder.headless(ctx(), RunRequest(prompt="", session_id="S1")),
            builder.interactive(ctx()),
        ):
            assert flag not in cmd


class TestBuiltinToolsGate:
    def test_absent_by_default(self, builder):
        assert "--no-builtin-tools" not in builder.headless(ctx(), RunRequest(prompt="hi"))

    def test_present_when_the_allowlisted_field_is_zero(self, builder):
        cmd = builder.headless(ctx(harness_config={"builtin_tools": "0"}), RunRequest(prompt="hi"))
        assert "--no-builtin-tools" in cmd

    @pytest.mark.parametrize(
        "value", ["0; rm -rf /", "--unsafe", "1 --unsafe", "$(whoami)", "1", "", "yes"]
    )
    def test_a_config_value_can_never_add_an_argument(self, builder, value):
        """The value selects a FIXED flag; it is never interpolated into an argument."""
        base = builder.headless(ctx(), RunRequest(prompt="hi"))
        cmd = builder.headless(ctx(harness_config={"builtin_tools": value}), RunRequest(prompt="hi"))
        assert cmd == base, f"{value!r} altered the command: {cmd}"


class TestParity:
    def test_interactive_matches_headless_apart_from_json_mode(self, builder):
        head = builder.headless(ctx(), RunRequest(prompt="hi"))
        inter = builder.interactive(ctx())
        assert "--mode" not in inter
        for flag in ("--offline", "--no-extensions", "-e", "--provider", "--model"):
            assert flag in head and flag in inter

    def test_yolo_is_a_no_op(self, builder):
        """Pi has no approval prompts by design, so there is nothing to bypass."""
        assert builder.interactive(ctx(), yolo=True) == builder.interactive(ctx(), yolo=False)


class TestStdinPayload:
    def test_a_normal_prompt_is_passed_through(self, builder):
        assert builder.stdin_payload(ctx(), RunRequest(prompt="do the thing")) == "do the thing"

    def test_a_leading_dash_prompt_survives_intact(self, builder):
        assert builder.stdin_payload(ctx(), RunRequest(prompt="-- review")) == "-- review"

    def test_resume_substitutes_a_NON_EMPTY_continuation_prompt(self, builder):
        """The consumer resumes with an empty prompt, and Pi's print mode only prompts
        when the initial message is non-empty — so an empty payload would open the
        session, print the header, and exit rc=0 having done nothing, which the job
        would record as SUCCESS. This is the guard against that."""
        payload = builder.stdin_payload(ctx(), RunRequest(prompt="", session_id="S1"))
        assert payload == RESUME_PROMPT
        assert payload, "an empty resume payload is a silent no-op"

    def test_interactive_has_no_payload(self, builder):
        assert builder.stdin_payload(ctx(), RunRequest(prompt="")) is None
