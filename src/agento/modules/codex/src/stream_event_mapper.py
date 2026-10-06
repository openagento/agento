"""Canonical fragments (E9 §3.2) from the Codex CLI's ``exec --json`` NDJSON stream.

Consumed by the framework's delta seam through the harness's ``stream_event_mapper``
member. Pure: one event in, one fragment out, no I/O. ``item.started`` and
``item.completed`` of one action share ``item.id``, which becomes the ``call_id``.
"""
from __future__ import annotations

import json


def _message(error: object) -> str:
    message = error.get("message") if isinstance(error, dict) else error
    return str(message or "")


class CodexStreamEventMapper:
    """``StreamEventMapper`` for Codex NDJSON events."""

    def map_event(self, event: dict) -> dict | None:
        kind = event.get("type")
        if kind == "turn.failed":
            return {"kind": "error", "text": _message(event.get("error")) or "turn failed"}
        if kind == "error":
            return {"kind": "error", "text": _message(event) or "unknown error"}
        item = event.get("item")
        if kind not in ("item.started", "item.completed") or not isinstance(item, dict):
            return None
        started = kind == "item.started"
        item_type = item.get("type")
        call_id = str(item.get("id") or "")

        if item_type == "agent_message":
            # Only the completed event carries the text.
            text = item.get("text")
            return None if started or not text else {"kind": "assistant.text", "text": str(text)}

        if item_type == "command_execution":
            if started:
                return {"kind": "tool.started", "tool_name": "shell",
                        "data": {"call_id": call_id, "input": str(item.get("command") or "")}}
            return {"kind": "tool.completed", "tool_name": "shell",
                    "data": {"call_id": call_id, "output": str(item.get("aggregated_output") or ""),
                             "is_error": item.get("exit_code") not in (0, None)}}

        if item_type == "mcp_tool_call":
            tool_name = str(item.get("tool") or "")
            if started:
                args = item.get("arguments")
                return {"kind": "tool.started", "tool_name": tool_name,
                        "data": {"call_id": call_id,
                                 "input": args if isinstance(args, str) else json.dumps(args)}}
            result = item.get("result")
            content = result.get("content") if isinstance(result, dict) else None
            output = "\n".join(
                b["text"] for b in content or []
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
            ) if isinstance(content, list) else ""
            error = item.get("error")
            return {"kind": "tool.completed", "tool_name": tool_name,
                    "data": {"call_id": call_id, "output": output or _message(error),
                             "is_error": bool(error)}}
        return None
