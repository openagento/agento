"""Golden test: CodexStreamEventMapper over a real captured ``codex exec --json`` run
(``real_success_with_mcp.ndjson``) — E9 §3.3."""
from __future__ import annotations

import json
from pathlib import Path

from agento.framework.harness.protocols import StreamEventMapper
from agento.modules.codex.src.adapter import CodexHarnessAdapter
from agento.modules.codex.src.stream_event_mapper import CodexStreamEventMapper

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "codex" / "real_success_with_mcp.ndjson"


def fragments(events: list[dict]) -> list[dict]:
    mapper, out = CodexStreamEventMapper(), []
    for event in events:
        mapped = mapper.map_event(event)
        out += mapped if isinstance(mapped, list) else [mapped] if mapped else []
    return out


def test_real_run_maps_to_the_exact_fragment_list():
    events = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    item = {e["item"]["id"]: e["item"] for e in events if e["type"] == "item.completed"}

    def shell_start(n: int) -> dict:
        return {"kind": "tool.started", "tool_name": "shell",
                "data": {"call_id": f"item_{n}", "input": item[f"item_{n}"]["command"]}}

    def shell_done(n: int) -> dict:
        return {"kind": "tool.completed", "tool_name": "shell",
                "data": {"call_id": f"item_{n}", "output": item[f"item_{n}"]["aggregated_output"],
                         "is_error": False}}

    def mcp_start(n: int) -> dict:
        return {"kind": "tool.started", "tool_name": "mysql_k3_magento_prod",
                "data": {"call_id": f"item_{n}",
                         "input": json.dumps(item[f"item_{n}"]["arguments"])}}

    def mcp_done(n: int) -> dict:
        return {"kind": "tool.completed", "tool_name": "mysql_k3_magento_prod",
                "data": {"call_id": f"item_{n}",
                         "output": item[f"item_{n}"]["result"]["content"][0]["text"],
                         "is_error": False}}

    def text(n: int) -> dict:
        return {"kind": "assistant.text", "text": item[f"item_{n}"]["text"]}

    assert fragments(events) == [
        text(0),
        shell_start(1), shell_start(2), shell_start(3), shell_start(4),
        shell_done(4), shell_done(3), shell_done(1), shell_done(2),
        text(5),
        mcp_start(6), mcp_done(6), mcp_start(7), mcp_done(7), mcp_start(8), mcp_done(8),
        text(9),
    ]


def test_failures_map_to_errors():
    assert fragments([
        {"type": "item.completed", "item": {"id": "c", "type": "command_execution",
                                            "command": "false", "aggregated_output": "",
                                            "exit_code": 1}},
        {"type": "item.completed", "item": {"id": "m", "type": "mcp_tool_call", "tool": "t",
                                            "result": None, "error": {"message": "denied"}}},
        {"type": "turn.failed", "error": {"message": "turn broke"}},
        {"type": "turn.failed", "error": "plain"},
        {"type": "error", "message": "stream broke"},
    ]) == [
        {"kind": "tool.completed", "tool_name": "shell",
         "data": {"call_id": "c", "output": "", "is_error": True}},
        {"kind": "tool.completed", "tool_name": "t",
         "data": {"call_id": "m", "output": "denied", "is_error": True}},
        {"kind": "error", "text": "turn broke"},
        {"kind": "error", "text": "plain"},
        {"kind": "error", "text": "stream broke"},
    ]


def test_the_adapter_exposes_the_mapper():
    mapper = CodexHarnessAdapter().stream_event_mapper
    assert isinstance(mapper, CodexStreamEventMapper)
    assert isinstance(mapper, StreamEventMapper)


def test_a_reasoning_summary_maps_to_reasoning():
    """codex 0.160 with `-c model_reasoning_summary=auto` (spike 2026-10-06)."""
    item = {"id": "item_2", "type": "reasoning", "text": "**Plan**\n\nSort first."}
    mapper = CodexStreamEventMapper()

    assert mapper.map_event({"type": "item.started", "item": item}) is None
    assert mapper.map_event({"type": "item.completed", "item": item}) == {
        "kind": "assistant.reasoning", "text": "**Plan**\n\nSort first."}
