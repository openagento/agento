"""Human-readable rendering of Claude Code's ``--output-format stream-json``.

Consumed by ``agento run --pretty`` through the harness's ``stream_renderer``
member. One event in, one printable block out (or ``None`` to hide it).
"""
from __future__ import annotations

from agento.framework.harness.stream_style import BRANCH, BULLET, bold, dim, truncate

# Argument worth showing next to a tool name, most specific first. A tool whose
# input has none of these falls back to the first short string value it carries.
_ARG_KEYS = (
    "command",
    "file_path",
    "notebook_path",
    "path",
    "pattern",
    "url",
    "query",
    "description",
    "prompt",
)


def _tool_hint(tool_input: object) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in _ARG_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return truncate(value, 80)
    for value in tool_input.values():
        if isinstance(value, str) and value.strip():
            return truncate(value, 80)
    return ""


def _content_blocks(event: dict) -> list:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def _result_text(block: dict) -> str:
    """Flatten a tool_result's ``content``, which is a str or a block list."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _format_duration(ms: object) -> str | None:
    if not isinstance(ms, (int, float)):
        return None
    return f"{ms / 1000:.1f}s"


class ClaudeStreamRenderer:
    """``StreamRenderer`` for Claude Code stream-json events."""

    def render(self, event: dict) -> str | None:
        if not isinstance(event, dict):
            return None
        kind = event.get("type")
        if kind == "system":
            return self._system(event)
        if kind == "assistant":
            return self._assistant(event)
        if kind == "user":
            return self._user(event)
        if kind == "result":
            return self._result(event)
        # An event type this renderer does not know still gets one dim line —
        # never raw JSON, and never silence that hides a stream format change.
        return dim(f"· {kind}") if kind else None

    def _system(self, event: dict) -> str | None:
        if event.get("subtype") != "init":
            return None
        parts = []
        if event.get("model"):
            parts.append(str(event["model"]))
        session = event.get("session_id")
        if session:
            parts.append(f"session {session}")
        tools = event.get("tools")
        if isinstance(tools, list):
            parts.append(f"{len(tools)} tools")
        return dim("· " + " · ".join(parts)) if parts else None

    def _assistant(self, event: dict) -> str | None:
        lines: list[str] = []
        for block in _content_blocks(event):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    lines.append(text)
            elif block.get("type") == "tool_use":
                name = str(block.get("name") or "tool")
                hint = _tool_hint(block.get("input"))
                lines.append(f"{BULLET} {bold(name)}({hint})")
        return "\n".join(lines) if lines else None

    def _user(self, event: dict) -> str | None:
        lines: list[str] = []
        for block in _content_blocks(event):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            text = truncate(_result_text(block))
            if block.get("is_error"):
                # Keep the message: what the tool said is why the run went wrong.
                text = f"error: {text}" if text else "error"
            lines.append(f"  {BRANCH} {dim(text or 'ok')}")
        return "\n".join(lines) if lines else None

    def _result(self, event: dict) -> str:
        if event.get("is_error"):
            return f"✗ {truncate(event.get('result') or 'unknown error', 400)}"
        parts = []
        turns = event.get("num_turns")
        if isinstance(turns, int):
            parts.append(f"{turns} turns")
        duration = _format_duration(event.get("duration_ms"))
        if duration:
            parts.append(duration)
        cost = event.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            parts.append(f"${cost:.4f}")
        suffix = f" · {' · '.join(parts)}" if parts else ""
        return f"✓ done{dim(suffix)}"
