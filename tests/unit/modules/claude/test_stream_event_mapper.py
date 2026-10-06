"""Golden test: ClaudeStreamEventMapper over a stream-json run that calls tools (E9 §3.3).

``tests/fixtures/claude/stream_tool_use.jsonl`` is SYNTHESIZED from the documented
stream-json shape (no live claude credential was available): a tool_result's ``content`` is
a string in one event and a list of text blocks in the other.
"""
from __future__ import annotations

import json
from pathlib import Path

from agento.framework.harness.protocols import StreamEventMapper
from agento.modules.claude.src.adapter import ClaudeHarnessAdapter
from agento.modules.claude.src.stream_event_mapper import ClaudeStreamEventMapper

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "claude" / "stream_tool_use.jsonl"


def fragments(raw: str) -> list[dict]:
    mapper, out = ClaudeStreamEventMapper(), []
    for line in raw.splitlines():
        mapped = mapper.map_event(json.loads(line))
        out += mapped if isinstance(mapped, list) else [mapped] if mapped else []
    return out


def test_tool_use_run_maps_to_the_exact_fragment_list():
    assert fragments(FIXTURE.read_text()) == [
        {"kind": "assistant.text", "text": "I will read the ticket."},
        {"kind": "tool.started", "tool_name": "mcp__toolbox__jira_get_issue",
         "data": {"call_id": "toolu_01", "input": json.dumps({"key": "DEMO-1"})}},
        {"kind": "tool.completed",
         "data": {"call_id": "toolu_01", "output": "Summary: Fix the login page",
                  "is_error": False}},
        {"kind": "tool.started", "tool_name": "Bash",
         "data": {"call_id": "toolu_02", "input": json.dumps({"command": "ls missing"})}},
        {"kind": "tool.completed",
         "data": {"call_id": "toolu_02", "output": "ls: missing: No such file\nexit 1",
                  "is_error": True}},
        {"kind": "assistant.text",
         "text": "The ticket asks to fix the login page.\nNo local files exist yet."},
    ]


def test_an_error_result_maps_to_error():
    mapped = ClaudeStreamEventMapper().map_event(
        {"type": "result", "is_error": True, "result": "Not logged in"})
    assert mapped == {"kind": "error", "text": "Not logged in"}


def test_the_adapter_exposes_the_mapper():
    mapper = ClaudeHarnessAdapter().stream_event_mapper
    assert isinstance(mapper, ClaudeStreamEventMapper)
    assert isinstance(mapper, StreamEventMapper)
