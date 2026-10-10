"""Canonical fragments (E9 §3.2) from Pi's ``--mode json`` NDJSON stream.

Consumed by the framework's delta seam through the harness's ``stream_event_mapper``
member. Pure: one event in, fragments out, no I/O. Only assistant messages become text,
so a user turn never shows as the agent's words.
"""
from __future__ import annotations

import json

from .output_parser import message_text


class PiStreamEventMapper:
    """``StreamEventMapper`` for Pi NDJSON events."""

    def map_event(self, event: dict) -> dict | list[dict] | None:
        kind = event.get("type")
        tool_name = str(event.get("toolName") or "")
        call_id = str(event.get("toolCallId") or "")

        if kind == "message_update":
            # Live text: delta-only events, sent before the message's `message_end`, which
            # supersedes them (docs/json.md of the pinned pi; plan F20).
            update = event.get("assistantMessageEvent")
            delta = update.get("delta") if isinstance(update, dict) else None
            if not isinstance(delta, str):
                return None
            partial = {"text_delta": "assistant.partial", "thinking_delta": "reasoning.partial"}
            return {"kind": partial[update["type"]], "text": delta} \
                if update.get("type") in partial else None

        if kind == "message_end":
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                return None
            out: list[dict] = [
                {"kind": "assistant.reasoning", "text": block["thinking"]}
                for block in message.get("content") or []
                if isinstance(block, dict) and block.get("type") == "thinking"
                and isinstance(block.get("thinking"), str)]
            if text := message_text(message):
                out.append({"kind": "assistant.text", "text": text})
            if error := message.get("errorMessage"):
                out.append({"kind": "error", "text": str(error)})
            return out

        if kind == "tool_execution_start":
            return {"kind": "tool.started", "tool_name": tool_name,
                    "data": {"call_id": call_id, "input": json.dumps(event.get("args"))}}

        if kind == "tool_execution_end":
            result = event.get("result")
            result = result if isinstance(result, dict) else {}
            return {"kind": "tool.completed", "tool_name": tool_name,
                    "data": {"call_id": call_id, "output": message_text(result),
                             "is_error": bool(event.get("isError") or result.get("isError"))}}
        return None
