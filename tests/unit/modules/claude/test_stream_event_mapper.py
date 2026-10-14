"""Golden test: ClaudeStreamEventMapper over a stream-json run that calls tools (E9 §3.3).

``tests/fixtures/claude/stream_tool_use.jsonl`` is SYNTHESIZED from the documented
stream-json shape (no live claude credential was available): a tool_result's ``content`` is
a string in one event and a list of text blocks in the other.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.framework.harness.protocols import StreamEventMapper
from agento.modules.claude.src.adapter import ClaudeHarnessAdapter
from agento.modules.claude.src.output_parser import parse_claude_output
from agento.modules.claude.src.stream_event_mapper import ClaudeStreamEventMapper
from agento.modules.claude.src.stream_renderer import ClaudeStreamRenderer

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "claude" / "stream_tool_use.jsonl"


def fragments(raw: str) -> list[dict]:
    mapper, out = ClaudeStreamEventMapper(), []
    for line in raw.splitlines():
        mapped = mapper.map_event(json.loads(line))
        out += mapped if isinstance(mapped, list) else [mapped] if mapped else []
    return out


def test_tool_use_run_maps_to_the_exact_fragment_list():
    assert fragments(FIXTURE.read_text()) == [
        {"kind": "assistant.reasoning", "text": "Read the ticket first."},
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


# A resume of a session claude cannot find: no `result`, the message only in `errors`.
# The exact event seen on a dev stack (job 26983), which showed as "unknown error".
_NO_SESSION = {
    "type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
    "session_id": "a8390dba-a29e-4f18-9498-79b7ccc4bbdc",
    "errors": ["No conversation found with session ID: a8390dba-a29e-4f18-9498-79b7ccc4bbdc"],
}


def test_an_error_without_result_text_shows_claudes_errors_everywhere():
    want = "No conversation found with session ID"
    assert want in ClaudeStreamEventMapper().map_event(_NO_SESSION)["text"]
    assert want in (ClaudeStreamRenderer().render(_NO_SESSION) or "")
    single = {k: v for k, v in _NO_SESSION.items() if k != "type"}  # old --output-format json
    for raw in (json.dumps(_NO_SESSION), json.dumps(single)):
        with pytest.raises(Exception, match=want):
            parse_claude_output(raw)


PARTIAL = FIXTURE.parent / "partial_messages.jsonl"


def test_partial_messages_map_to_live_text_then_the_complete_block():
    """Trimmed from a live `--include-partial-messages` run (claude 2.1.291): the deltas
    come first and the block's complete `assistant` event after them (plan F20)."""
    out = fragments(PARTIAL.read_text())
    kinds = [f["kind"] for f in out]

    assert kinds[:3] == ["reasoning.partial", "reasoning.partial", "assistant.reasoning"]
    assert set(kinds[3:-1]) == {"assistant.partial"} and kinds[-1] == "assistant.text"
    assert "".join(f["text"] for f in out if f["kind"] == "assistant.partial") == out[-1]["text"]


def test_successive_blocks_and_a_cut_off_block_keep_stream_order():
    def delta(kind, text):
        field = "text" if kind == "text_delta" else "thinking"
        return {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                  "delta": {"type": kind, field: text}}}
    raw = [delta("thinking_delta", "a"),
           {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "a"}]}},
           delta("text_delta", "Hi"),
           {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}]}},
           delta("text_delta", "cut")]                     # the run ends mid-block
    out = fragments("\n".join(json.dumps(e) for e in raw))

    assert [(f["kind"], f["text"]) for f in out] == [
        ("reasoning.partial", "a"), ("assistant.reasoning", "a"),
        ("assistant.partial", "Hi"), ("assistant.text", "Hi"), ("assistant.partial", "cut")]


def test_other_stream_events_map_to_nothing():
    for event in ({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
                  {"type": "content_block_delta", "delta": {"type": "signature_delta"}},
                  {"type": "message_stop"}):
        assert ClaudeStreamEventMapper().map_event({"type": "stream_event", "event": event}) is None


def test_the_parser_and_renderer_ignore_partial_lines():
    raw = PARTIAL.read_text()
    assert parse_claude_output(raw).raw_output == parse_claude_output(
        "\n".join(line for line in raw.splitlines() if '"stream_event"' not in line)).raw_output
    partials = [json.loads(line) for line in raw.splitlines() if '"stream_event"' in line]
    assert not any(ClaudeStreamRenderer().render(e) for e in partials)
