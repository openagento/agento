"""Codex-specific implementation of the framework ``TranscriptReader`` protocol.

Codex writes session transcripts as JSONL under
``$HOME/.codex/sessions/YYYY/MM/DD/rollout-<iso8601>-<session_uuid>.jsonl``.
In Agento, agents run with ``HOME`` set to the per-agent-view build directory
under ``BUILD_DIR/<workspace_code>/<agent_view_code>/<build_id>/`` (with
``.codex/sessions`` further symlinked to a sibling ``state/.codex/sessions``
by ``workspace_build``). Search is therefore rooted at ``BUILD_DIR`` with a
leading recursive glob to walk the build-dir nesting in addition to the
``YYYY/MM/DD`` inner walk. The session UUID is what the codex CLI prints to
stderr as ``session id: <uuid>`` and is captured into ``RunResult.subtype``
by the codex runner.

Each line is a JSON object of the form
``{"timestamp": ..., "type": ..., "payload": {...}}``. Tool invocations appear
as ``response_item`` records with ``payload.type == "function_call"``:

    {"type":"response_item","payload":{
        "type":"function_call","name":..., "namespace":..., "call_id":..., "arguments":...
    }}

MCP detection follows three rules, in priority order:

1. **Modern** — ``payload.namespace`` starts with ``mcp__`` → qualified name
   is ``namespace + name`` (e.g. ``mcp__toolbox__jira_search``).
2. **Event-correlated** — a sibling ``event_msg`` with
   ``payload.type == "mcp_tool_call_end"`` and a matching ``call_id`` covers
   MCP meta-calls (e.g. ``list_mcp_resources``) that lack a namespace.
   Qualified name is ``mcp__<server>__<tool>`` taken from
   ``payload.invocation``.
3. **Legacy** (Codex ≤ 0.81) — ``payload.name`` already starts with ``mcp__``
   → emit as-is.

Non-MCP local tool calls (``exec_command``, ``apply_patch``, …) are also
yielded — MCP-filtering is the observer's job, not ours. A record is
"recognized" when it has ``type`` in ``{"response_item", "event_msg",
"turn_context", "compacted"}`` (the known Codex outer envelope). Lines whose
JSON parses but doesn't match this shape are counted toward
``ParseSummary.total_json_lines`` only — letting callers detect silent format
drift when ``recognized_records == 0`` despite a non-empty file.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path

from agento.framework.harness import ParseSummary, ToolUse
from agento.framework.workspace_paths import BUILD_DIR

logger = logging.getLogger(__name__)

_RECOGNIZED_TYPES = frozenset({
    "session_meta", "turn_context", "response_item", "event_msg", "compacted",
})


def _find_transcript(session_id: str, search_root: Path) -> Path:
    pattern = f"**/.codex/sessions/**/rollout-*{session_id}.jsonl"
    matches = list(search_root.glob(pattern))
    if not matches:
        raise FileNotFoundError(
            f"No codex transcript for session_id={session_id} under {search_root}"
        )
    if len(matches) > 1:
        logger.warning(
            "Multiple codex transcripts for session_id=%s under %s: %s",
            session_id, search_root, [str(m) for m in matches],
        )
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def _iter_records(path: Path) -> Iterable[dict]:
    with path.open() as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("Skipping malformed JSONL line %s in %s", line_no, path)
                continue
            if isinstance(row, dict):
                yield row


class CodexTranscriptReader:
    """Parse codex session JSONL transcripts."""

    def _search_root(self) -> Path:
        return Path(BUILD_DIR)

    def parse(self, session_id: str) -> ParseSummary:
        path = _find_transcript(session_id, self._search_root())

        rows = list(_iter_records(path))
        total_json_lines = len(rows)
        recognized_records = sum(1 for r in rows if r.get("type") in _RECOGNIZED_TYPES)

        # Pass 1: collect call_id → invocation for mcp_tool_call_end events.
        mcp_events: dict[str, dict] = {}
        for row in rows:
            if row.get("type") != "event_msg":
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "mcp_tool_call_end":
                continue
            call_id = payload.get("call_id")
            invocation = payload.get("invocation")
            if isinstance(call_id, str) and isinstance(invocation, dict):
                mcp_events[call_id] = invocation

        # Pass 2: yield ToolUse for every function_call.
        tool_uses: list[ToolUse] = []
        for row in rows:
            if row.get("type") != "response_item":
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "function_call":
                continue
            call_id = payload.get("call_id")
            name = payload.get("name")
            if not isinstance(call_id, str) or not isinstance(name, str):
                continue

            namespace = payload.get("namespace")
            if isinstance(namespace, str) and namespace.startswith("mcp__"):
                qualified = f"{namespace}{name}"
            elif call_id in mcp_events:
                inv = mcp_events[call_id]
                server = inv.get("server")
                tool = inv.get("tool")
                qualified = (
                    f"mcp__{server}__{tool}"
                    if isinstance(server, str) and isinstance(tool, str)
                    else name
                )
            else:
                qualified = name

            tool_uses.append(ToolUse(name=qualified, tool_use_id=call_id))

        return ParseSummary(
            total_json_lines=total_json_lines,
            recognized_records=recognized_records,
            tool_uses=tuple(tool_uses),
        )

    def iter_tool_uses(self, session_id: str) -> tuple[ToolUse, ...]:
        return self.parse(session_id).tool_uses
