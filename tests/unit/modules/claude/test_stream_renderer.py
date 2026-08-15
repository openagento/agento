"""Tests for ClaudeStreamRenderer — `agento run --pretty` line rendering."""
from __future__ import annotations

import pytest

from agento.modules.claude.src.stream_renderer import ClaudeStreamRenderer


@pytest.fixture
def renderer():
    return ClaudeStreamRenderer()


def _assistant(*blocks):
    return {"type": "assistant", "message": {"content": list(blocks)}}


class TestSystemInit:
    def test_init_renders_one_header_line(self, renderer):
        out = renderer.render(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "abc-123",
                "model": "claude-opus-4-6",
                "tools": ["Read", "Bash", "Edit"],
            }
        )
        assert out is not None
        assert "claude-opus-4-6" in out
        assert "session abc-123" in out
        assert "3 tools" in out
        assert "\n" not in out

    def test_non_init_system_event_is_suppressed(self, renderer):
        assert renderer.render({"type": "system", "subtype": "other"}) is None


class TestAssistant:
    def test_text_block_renders_its_text(self, renderer):
        out = renderer.render(_assistant({"type": "text", "text": "  Hello there  "}))
        assert out == "Hello there"

    def test_empty_text_block_is_suppressed(self, renderer):
        assert renderer.render(_assistant({"type": "text", "text": "   "})) is None

    def test_string_content_is_treated_as_text(self, renderer):
        event = {"type": "assistant", "message": {"content": "plain answer"}}
        assert renderer.render(event) == "plain answer"

    def test_tool_use_renders_name_and_argument_hint(self, renderer):
        out = renderer.render(
            _assistant(
                {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}
            )
        )
        assert out is not None
        assert "Bash(git status)" in out
        assert out.startswith("⏺")

    def test_tool_use_prefers_the_most_specific_argument(self, renderer):
        out = renderer.render(
            _assistant(
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"offset": 10, "file_path": "/src/app.py"},
                }
            )
        )
        assert "Read(/src/app.py)" in out

    def test_tool_use_falls_back_to_any_string_argument(self, renderer):
        out = renderer.render(
            _assistant({"type": "tool_use", "name": "Custom", "input": {"zzz": "value"}})
        )
        assert "Custom(value)" in out

    def test_tool_use_with_no_usable_argument_renders_empty_parens(self, renderer):
        out = renderer.render(
            _assistant({"type": "tool_use", "name": "CronList", "input": {}})
        )
        assert "CronList()" in out

    def test_long_argument_is_truncated(self, renderer):
        out = renderer.render(
            _assistant(
                {"type": "tool_use", "name": "Bash", "input": {"command": "x" * 500}}
            )
        )
        assert len(out) < 200
        assert "…" in out

    def test_text_and_tool_use_render_as_separate_lines(self, renderer):
        out = renderer.render(
            _assistant(
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            )
        )
        assert out.splitlines() == ["Let me look.", "⏺ Bash(ls)"]


class TestUserToolResult:
    def test_tool_result_renders_a_truncated_continuation_line(self, renderer):
        event = {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "content": "file one\nfile two"}]
            },
        }
        out = renderer.render(event)
        assert out is not None
        assert "⎿" in out
        # First line only; the rest is counted, never silently dropped.
        assert "file one (+1 line)" in out
        assert "file two" not in out

    def test_block_list_content_is_flattened(self, renderer):
        event = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": [{"type": "text", "text": "ok then"}],
                    }
                ]
            },
        }
        assert "ok then" in renderer.render(event)

    def test_error_result_is_labelled_and_keeps_its_message(self, renderer):
        event = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": "fatal: not a git repository",
                        "is_error": True,
                    }
                ]
            },
        }
        out = renderer.render(event)
        assert "error" in out
        # The tool's own message is why the run went wrong — never drop it.
        assert "fatal: not a git repository" in out

    def test_error_result_without_content_still_says_error(self, renderer):
        event = {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "is_error": True}]},
        }
        assert "error" in renderer.render(event)

    def test_user_event_without_a_tool_result_is_suppressed(self, renderer):
        event = {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}
        assert renderer.render(event) is None


class TestResult:
    def test_success_summarises_turns_duration_and_cost(self, renderer):
        out = renderer.render(
            {
                "type": "result",
                "is_error": False,
                "num_turns": 3,
                "duration_ms": 12400,
                "total_cost_usd": 0.0812,
            }
        )
        assert out.startswith("✓ done")
        assert "3 turns" in out
        assert "12.4s" in out
        assert "$0.0812" in out

    def test_success_without_metrics_still_reports_done(self, renderer):
        assert renderer.render({"type": "result"}) == "✓ done"

    def test_error_result_shows_the_message(self, renderer):
        out = renderer.render(
            {"type": "result", "is_error": True, "result": "credit balance too low"}
        )
        assert out.startswith("✗")
        assert "credit balance too low" in out


class TestUnknownEvents:
    def test_unknown_type_renders_one_dim_line_not_raw_json(self, renderer):
        out = renderer.render({"type": "brand_new_event", "payload": {"secret": 1}})
        assert out is not None
        assert "brand_new_event" in out
        assert "secret" not in out

    def test_event_without_a_type_is_suppressed(self, renderer):
        assert renderer.render({"no": "type"}) is None

    def test_non_dict_event_is_suppressed(self, renderer):
        assert renderer.render("nope") is None
