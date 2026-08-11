"""Claude-specific implementation of the framework ``TranscriptReader`` protocol.

Claude Code writes session transcripts as JSONL under
``$HOME/.claude/projects/<mangled-cwd>/<session_id>.jsonl``. In Agento, agents
run with ``HOME`` set to the per-agent-view build directory under
``BUILD_DIR/<workspace_code>/<agent_view_code>/<build_id>/`` (with
``.claude/projects`` further symlinked to a sibling ``state/.claude/projects``
by ``workspace_build``). Search is therefore rooted at ``BUILD_DIR`` and uses
a recursive glob — session IDs are UUIDs so cross-collisions aren't a
practical concern.

Each transcript line is a JSON object. Tool invocations appear as
``{"message": {"content": [{"type": "tool_use", "name": ..., "id": ...}]}}``.
A record is "recognized" when ``message.content`` is a list (the outer Claude
shape). Lines whose JSON parses but doesn't match this shape are counted
toward ``ParseSummary.total_json_lines`` only — letting callers detect silent
format drift when ``recognized_records == 0`` despite a non-empty file.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from agento.framework.harness import ParseSummary, ToolUse
from agento.framework.workspace_paths import BUILD_DIR

logger = logging.getLogger(__name__)


def _find_transcript(session_id: str, search_root: Path) -> Path:
    pattern = f"**/.claude/projects/*/{session_id}.jsonl"
    matches = list(search_root.glob(pattern))
    if not matches:
        raise FileNotFoundError(
            f"No claude transcript for session_id={session_id} under {search_root}"
        )
    if len(matches) > 1:
        logger.warning(
            "Multiple claude transcripts for session_id=%s under %s: %s",
            session_id, search_root, [str(m) for m in matches],
        )
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


class ClaudeTranscriptReader:
    """Parse Claude Code session JSONL transcripts."""

    def _search_root(self) -> Path:
        return Path(BUILD_DIR)

    def parse(self, session_id: str) -> ParseSummary:
        path = _find_transcript(session_id, self._search_root())
        total_json_lines = 0
        recognized_records = 0
        tool_uses: list[ToolUse] = []
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
                total_json_lines += 1
                if not isinstance(row, dict):
                    continue
                message = row.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                recognized_records += 1
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") != "tool_use":
                        continue
                    name = item.get("name")
                    tu_id = item.get("id")
                    if isinstance(name, str) and isinstance(tu_id, str):
                        tool_uses.append(ToolUse(name=name, tool_use_id=tu_id))
        return ParseSummary(
            total_json_lines=total_json_lines,
            recognized_records=recognized_records,
            tool_uses=tuple(tool_uses),
        )

    def iter_tool_uses(self, session_id: str) -> tuple[ToolUse, ...]:
        return self.parse(session_id).tool_uses
