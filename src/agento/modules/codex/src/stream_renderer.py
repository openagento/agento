"""Human-readable rendering of the Codex CLI's ``--json`` NDJSON stream.

Consumed by ``agento run --pretty`` through the harness's ``stream_renderer``
member. Codex emits an ``item.started``/``item.completed`` pair per action, so
the start renders the call line and the completion renders its result — the
same two-line shape the Claude renderer produces.
"""
from __future__ import annotations

from agento.framework.harness.stream_style import BRANCH, BULLET, bold, dim, truncate


def _format_usage(usage: object) -> str:
    if not isinstance(usage, dict):
        return ""
    inp = usage.get("input_tokens")
    out = (usage.get("output_tokens") or 0) + (usage.get("reasoning_output_tokens") or 0)
    parts = []
    if isinstance(inp, int):
        parts.append(f"{inp} in")
    if out:
        parts.append(f"{out} out")
    return " · ".join(parts)


class CodexStreamRenderer:
    """``StreamRenderer`` for Codex NDJSON events."""

    def render(self, event: dict) -> str | None:
        if not isinstance(event, dict):
            return None
        kind = event.get("type")
        if kind == "thread.started":
            thread = event.get("thread_id")
            return dim(f"· session {thread}") if thread else None
        if kind == "turn.started":
            return None
        if kind == "turn.completed":
            usage = _format_usage(event.get("usage"))
            return f"✓ done{dim(' · ' + usage)}" if usage else "✓ done"
        if kind == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            return f"✗ {truncate(message or 'turn failed', 400)}"
        if kind == "error":
            return f"✗ {truncate(event.get('message') or 'unknown error', 400)}"
        if kind in ("item.started", "item.completed"):
            return self._item(event, completed=kind == "item.completed")
        return dim(f"· {kind}") if kind else None

    def _item(self, event: dict, *, completed: bool) -> str | None:
        item = event.get("item")
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")

        if item_type == "agent_message":
            # Only the completed event carries the final text.
            if not completed:
                return None
            text = str(item.get("text") or "").strip()
            return text or None

        if item_type == "command_execution":
            if not completed:
                return f"{BULLET} {bold('Bash')}({truncate(item.get('command') or '', 80)})"
            return f"  {BRANCH} {dim(self._command_result(item))}"

        if item_type == "mcp_tool_call":
            if not completed:
                name = f"{item.get('server')}.{item.get('tool')}"
                return f"{BULLET} {bold(name)}({truncate(item.get('arguments') or '', 80)})"
            # Same rule as a failing command: keep whatever the call said went
            # wrong, never collapse it to a bare status word.
            status = str(item.get("status") or "done")
            error = item.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            if message:
                status = f"{status} · {truncate(message, 100)}"
            return f"  {BRANCH} {dim(status)}"

        # Unknown item type: one dim line on completion so a stream format
        # change is visible without dumping raw JSON.
        return dim(f"· {item_type}") if completed and item_type else None

    def _command_result(self, item: dict) -> str:
        exit_code = item.get("exit_code")
        output = truncate(item.get("aggregated_output") or "", 100)
        if not isinstance(exit_code, int):
            # No code reported (a still-running or malformed item).
            return output or "done"
        return f"exit {exit_code} · {output}" if output else f"exit {exit_code}"
