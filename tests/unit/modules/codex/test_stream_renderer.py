"""Tests for CodexStreamRenderer — `agento run --pretty` line rendering."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.modules.codex.src.stream_renderer import CodexStreamRenderer

FIXTURE = (
    Path(__file__).parents[3] / "fixtures" / "codex" / "real_success_with_mcp.ndjson"
)


@pytest.fixture
def renderer():
    return CodexStreamRenderer()


class TestThreadAndTurn:
    def test_thread_started_renders_a_session_header(self, renderer):
        out = renderer.render({"type": "thread.started", "thread_id": "019e-585e"})
        assert out is not None
        assert "session 019e-585e" in out

    def test_turn_started_is_suppressed(self, renderer):
        assert renderer.render({"type": "turn.started"}) is None

    def test_turn_completed_summarises_usage(self, renderer):
        out = renderer.render(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 101854,
                    "output_tokens": 1904,
                    "reasoning_output_tokens": 833,
                },
            }
        )
        assert out.startswith("✓ done")
        assert "101854 in" in out
        assert "2737 out" in out

    def test_turn_completed_without_usage_still_reports_done(self, renderer):
        assert renderer.render({"type": "turn.completed"}) == "✓ done"

    def test_turn_failed_shows_the_error(self, renderer):
        out = renderer.render(
            {"type": "turn.failed", "error": {"message": "rate limited"}}
        )
        assert out.startswith("✗")
        assert "rate limited" in out

    def test_error_event_shows_the_message(self, renderer):
        out = renderer.render({"type": "error", "message": "stream broke"})
        assert out.startswith("✗")
        assert "stream broke" in out


class TestItems:
    def test_agent_message_renders_only_on_completion(self, renderer):
        item = {"id": "i0", "type": "agent_message", "text": "Będę czytał LESSONS.md"}
        assert renderer.render({"type": "item.started", "item": item}) is None
        out = renderer.render({"type": "item.completed", "item": item})
        assert out == "Będę czytał LESSONS.md"

    def test_command_execution_start_renders_the_call(self, renderer):
        out = renderer.render(
            {
                "type": "item.started",
                "item": {
                    "type": "command_execution",
                    "command": "/bin/bash -lc \"sed -n '1,220p' LESSONS.md\"",
                    "status": "in_progress",
                },
            }
        )
        assert out.startswith("⏺")
        assert "Bash(" in out
        assert "LESSONS.md" in out

    def test_command_execution_completion_renders_its_output(self, renderer):
        out = renderer.render(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "ls",
                    "aggregated_output": "README.md\nsrc\n",
                    "exit_code": 0,
                },
            }
        )
        assert "⎿" in out
        assert "exit 0" in out
        # First line only; the trailing lines are counted, not printed.
        assert "README.md (+1 line)" in out
        assert "src" not in out

    def test_silent_successful_command_still_reports_exit_0(self, renderer):
        out = renderer.render(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "true",
                    "aggregated_output": "",
                    "exit_code": 0,
                },
            }
        )
        assert "exit 0" in out

    def test_failing_command_shows_its_exit_code(self, renderer):
        out = renderer.render(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "false",
                    "aggregated_output": "",
                    "exit_code": 3,
                },
            }
        )
        assert "exit 3" in out

    def test_mcp_tool_call_start_renders_server_and_tool(self, renderer):
        out = renderer.render(
            {
                "type": "item.started",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "toolbox",
                    "tool": "mysql_k3_magento_prod",
                    "arguments": {"query": "SELECT 1"},
                },
            }
        )
        assert "toolbox.mysql_k3_magento_prod" in out

    def test_mcp_tool_call_completion_renders_its_status(self, renderer):
        out = renderer.render(
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "toolbox",
                    "tool": "x",
                    "status": "completed",
                },
            }
        )
        assert "⎿" in out
        assert "completed" in out

    def test_failing_mcp_tool_call_keeps_its_error_message(self, renderer):
        out = renderer.render(
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "toolbox",
                    "tool": "x",
                    "status": "failed",
                    "error": {"message": "connection refused"},
                },
            }
        )
        assert "failed" in out
        assert "connection refused" in out

    def test_unknown_item_type_renders_one_dim_line_not_raw_json(self, renderer):
        out = renderer.render(
            {"type": "item.completed", "item": {"type": "todo_list", "items": ["a"]}}
        )
        assert out is not None
        assert "todo_list" in out
        assert "items" not in out


class TestUnknownEvents:
    def test_unknown_top_level_type_renders_one_dim_line(self, renderer):
        out = renderer.render({"type": "brand.new", "payload": {"secret": 1}})
        assert "brand.new" in out
        assert "secret" not in out

    def test_non_dict_event_is_suppressed(self, renderer):
        assert renderer.render(["not", "a", "dict"]) is None


class TestRealFixture:
    """The whole recorded stream renders without raising, and hides nothing real."""

    def test_every_line_renders(self, renderer):
        events = [
            json.loads(line)
            for line in FIXTURE.read_text().splitlines()
            if line.strip()
        ]
        rendered = [renderer.render(event) for event in events]
        assert len(events) > 10
        # Every rendered value is either a printable string or a deliberate None.
        assert all(r is None or isinstance(r, str) for r in rendered)
        # The agent's own messages and its command calls both survive rendering.
        text = "\n".join(r for r in rendered if r)
        assert "⏺" in text
        assert "✓ done" in text
