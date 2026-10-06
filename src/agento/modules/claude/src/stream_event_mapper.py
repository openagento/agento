"""Canonical fragments (E9 §3.2) from Claude Code's ``--output-format stream-json``.

Consumed by the framework's delta seam through the harness's ``stream_event_mapper``
member. Pure: one event in, fragments out, no I/O.
"""
from __future__ import annotations

import json

from agento.modules.claude.src.output_parser import error_text


def _text(blocks: object) -> str:
    if not isinstance(blocks, list):
        return ""
    return "\n".join(
        b["text"] for b in blocks
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        and b["text"]
    )


class ClaudeStreamEventMapper:
    """``StreamEventMapper`` for Claude stream-json events."""

    def map_event(self, event: dict) -> dict | list[dict] | None:
        kind = event.get("type")
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []

        if kind == "assistant":
            out: list[dict] = []
            if text := _text(blocks):
                out.append({"kind": "assistant.text", "text": text})
            out += [
                {"kind": "tool.started", "tool_name": str(b.get("name") or ""),
                 "data": {"call_id": str(b.get("id") or ""), "input": json.dumps(b.get("input"))}}
                for b in blocks if b.get("type") == "tool_use"
            ]
            return out

        if kind == "user":
            # A tool_result's content is a plain string or a list of text blocks.
            return [
                {"kind": "tool.completed",
                 "data": {"call_id": str(b.get("tool_use_id") or ""),
                          "output": b["content"] if isinstance(b.get("content"), str)
                          else _text(b.get("content")),
                          "is_error": bool(b.get("is_error"))}}
                for b in blocks if b.get("type") == "tool_result"
            ]

        if kind == "result" and event.get("is_error"):
            return {"kind": "error", "text": error_text(event)}
        return None
