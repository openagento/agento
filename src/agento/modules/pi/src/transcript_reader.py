"""Pi-specific implementation of the framework ``TranscriptReader`` protocol.

Pi stores sessions as JSONL under ``$HOME/.pi/agent/sessions/<cwd-slug>/…`` with the
session id embedded in the filename. In Agento the agent runs with ``HOME`` set to the
per-agent-view build directory, and ``.pi/agent/sessions`` is symlinked to a sibling
persistent ``state/`` directory by ``workspace_build`` (declared through
``persistent_home_paths``). Search is therefore rooted at ``BUILD_DIR`` with a leading
recursive glob.

**Located by glob on the session id, never by byte offset.** Pi's ``_rewriteFile()``
reopens the transcript with flag ``"w"`` and rewrites it whole — compaction and branch
summaries rewrite history in place — so anything that tailed by offset would silently
desynchronise.

Tool names are taken verbatim. The bridge registers Toolbox tools as
``mcp__toolbox__<name>`` precisely so no translation is needed here and
``app_monitor``'s ``t.name.startswith("mcp__toolbox__")`` telemetry works unchanged.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path

from agento.framework.harness import ParseSummary, ToolUse

logger = logging.getLogger(__name__)

# Outer envelope shapes Pi writes. A parseable line outside this set counts toward
# total_json_lines only, so `recognized_records == 0` on a non-empty file is the
# canonical "Pi changed its transcript format" signal.
RECOGNIZED_TYPES = frozenset(
    {"message", "compaction", "branch_summary", "custom", "model_change", "session"}
)

# The bridge's session-start entry, the source for the MCP init report.
TOOLBOX_INIT_RECORD = "agento-toolbox-init"
MODEL_MISMATCH_RECORD = "agento-model-mismatch"


def _build_root() -> Path:
    """The framework's build root — the ONE the rest of Agento uses.

    An earlier version read an ``AGENTO_BUILD_DIR`` env var defaulting to
    ``/var/agento/builds``. That variable is set by nothing and defined nowhere else in
    the repo, and the default path exists in no container, so every lookup in a real
    deployment missed and both transcript-derived fields (``job.toolbox_mcp_connected``
    and ``job.toolbox_mcp_calls``) stayed NULL. Every unit test passed
    ``build_root=tmp_path`` explicitly, so none of them ever exercised the default —
    found by running a real job through the consumer queue.

    ``workspace_paths.BUILD_DIR`` derives from ``AGENTO_WORKSPACE_DIR``, which is the
    knob that actually exists.
    """
    from agento.framework.workspace_paths import BUILD_DIR

    return Path(BUILD_DIR)


class PiTranscriptReader:
    def __init__(self, build_root: Path | None = None) -> None:
        self._build_root = build_root

    def _root(self) -> Path:
        return self._build_root if self._build_root is not None else _build_root()

    def _find(self, session_id: str) -> Path:
        """Glob for the transcript carrying this session id."""
        if not session_id:
            raise FileNotFoundError("No Pi session id given")
        matches = sorted(
            self._root().glob(f"**/.pi/agent/sessions/**/*{session_id}*.jsonl")
        )
        if not matches:
            raise FileNotFoundError(
                f"No Pi transcript found for session {session_id!r} under {self._root()}"
            )
        # Newest wins: a resumed job reuses the id, and compaction may leave siblings.
        return max(matches, key=lambda p: p.stat().st_mtime)

    def _iter_records(self, session_id: str) -> Iterator[tuple[bool, dict]]:
        path = self._find(session_id)
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                yield record.get("type") in RECOGNIZED_TYPES, record

    def parse(self, session_id: str) -> ParseSummary:
        total = 0
        recognized = 0
        tool_uses: list[ToolUse] = []
        for is_recognized, record in self._iter_records(session_id):
            total += 1
            if is_recognized:
                recognized += 1
            tool_uses.extend(self._tool_uses_in(record))
        return ParseSummary(
            total_json_lines=total,
            recognized_records=recognized,
            tool_uses=tuple(tool_uses),
        )

    def iter_tool_uses(self, session_id: str) -> Iterable[ToolUse]:
        for _, record in self._iter_records(session_id):
            yield from self._tool_uses_in(record)

    def _tool_uses_in(self, record: dict) -> Iterator[ToolUse]:
        """Tool calls live in the content blocks of an assistant message."""
        if record.get("type") != "message":
            return
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "toolCall":
                continue
            name = block.get("name")
            if not isinstance(name, str) or not name:
                continue
            call_id = block.get("id") or block.get("toolCallId") or ""
            yield ToolUse(name=name, tool_use_id=str(call_id))

    def read_toolbox_init(self, session_id: str) -> dict | None:
        """The bridge's ``agento-toolbox-init`` entry, if it was written.

        `pi.appendEntry(customType, data)` stores a custom entry keyed by
        **``customType``** — not ``name`` — directly in the session JSONL. (The stdout
        stream wraps the same entry in ``{"type":"entry_appended","entry":…}``; that shape
        is handled by ``output_parser``.)
        """
        return self._read_custom(session_id, TOOLBOX_INIT_RECORD)

    def read_model_mismatch(self, session_id: str) -> dict | None:
        """The bridge's ``agento-model-mismatch`` entry, if the guard fired."""
        return self._read_custom(session_id, MODEL_MISMATCH_RECORD)

    def _read_custom(self, session_id: str, custom_type: str) -> dict | None:
        for _, record in self._iter_records(session_id):
            if record.get("type") != "custom":
                continue
            if record.get("customType") != custom_type:
                continue
            data = record.get("data")
            if isinstance(data, dict):
                return data
        return None
