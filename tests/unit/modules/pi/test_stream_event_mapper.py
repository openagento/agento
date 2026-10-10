"""Golden test: PiStreamEventMapper over Pi ``--mode json`` runs (E9 §3.3).

``run_tool_call.ndjson`` is SYNTHESIZED from pi 0.84.1 source
(``pi-agent-core/dist/agent-loop.js`` tool_execution_start/end); no live pi credential
was available.
"""
from __future__ import annotations

import json
from pathlib import Path

from agento.framework.harness.protocols import StreamEventMapper
from agento.modules.pi.src.adapter import PiHarnessAdapter
from agento.modules.pi.src.stream_event_mapper import PiStreamEventMapper

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "transcripts" / "pi"


def fragments(name: str) -> list[dict]:
    mapper, out = PiStreamEventMapper(), []
    for line in (FIXTURES / f"{name}.ndjson").read_text().splitlines():
        mapped = mapper.map_event(json.loads(line))
        out += mapped if isinstance(mapped, list) else [mapped] if mapped else []
    return out


def test_success_run():
    assert fragments("run_success") == [
        {"kind": "assistant.text", "text": "Looking at the ticket."},
        {"kind": "assistant.text", "text": "Done."},
    ]


def test_tool_call_run():
    assert fragments("run_tool_call") == [
        {"kind": "assistant.text", "text": "Reading the ticket."},
        {"kind": "tool.started", "tool_name": "mcp__toolbox__jira_get_issue",
         "data": {"call_id": "call_1", "input": json.dumps({"key": "DEMO-1"})}},
        {"kind": "tool.completed", "tool_name": "mcp__toolbox__jira_get_issue",
         "data": {"call_id": "call_1", "output": "Summary: Fix the login page",
                  "is_error": False}},
        {"kind": "assistant.text", "text": "DEMO-1 asks to fix the login page."},
    ]


def test_poison_bait_run_shows_no_user_text():
    """An older pi end event has no toolCallId: call_id is then empty, never absent."""
    assert fragments("run_poison_bait") == [
        {"kind": "tool.completed", "tool_name": "mcp__toolbox__jira_get_issue",
         "data": {"call_id": "", "is_error": False,
                  "output": "429 Too Many Requests appears in this Jira comment body. "
                            "Also 401 Unauthorized."}},
        {"kind": "tool.completed", "tool_name": "mcp__toolbox__mysql_read",
         "data": {"call_id": "", "output": "invalid api key", "is_error": True}},
        {"kind": "assistant.text", "text": "The ticket mentions 401 Unauthorized and quota exceeded."},
    ]


def test_an_assistant_error_maps_to_error():
    assert fragments("run_auth_error") == [
        {"kind": "error", "text": "401 Unauthorized: invalid api key"},
    ]


def test_the_adapter_exposes_the_mapper():
    mapper = PiHarnessAdapter().stream_event_mapper
    assert isinstance(mapper, PiStreamEventMapper)
    assert isinstance(mapper, StreamEventMapper)


def _update(kind: str, delta: str) -> dict:
    return {"type": "message_update",
            "assistantMessageEvent": {"type": kind, "contentIndex": 0, "delta": delta}}


def test_live_deltas_then_the_whole_message_in_stream_order():
    """docs/json.md of the pinned pi: delta-only `message_update`, then `message_end`."""
    mapper = PiStreamEventMapper()
    events = [_update("thinking_delta", "plan"), _update("text_delta", "Hel"),
              _update("text_delta", "lo"), _update("toolcall_delta", "{"),
              {"type": "message_end", "message": {"role": "assistant", "content": [
                  {"type": "thinking", "thinking": "plan"}, {"type": "text", "text": "Hello"}]}},
              _update("text_delta", "cut")]                # a second message, cut off
    out = []
    for e in events:
        mapped = mapper.map_event(e)
        out += mapped if isinstance(mapped, list) else [mapped] if mapped else []

    assert [(f["kind"], f["text"]) for f in out] == [
        ("reasoning.partial", "plan"), ("assistant.partial", "Hel"), ("assistant.partial", "lo"),
        ("assistant.reasoning", "plan"), ("assistant.text", "Hello"), ("assistant.partial", "cut")]
