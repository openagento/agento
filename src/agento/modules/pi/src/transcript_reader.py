"""Reads the Pi bridge's ``agento-toolbox-init`` entry from a Pi session transcript.

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
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

# The bridge's session-start entry, the source for the MCP init report.
TOOLBOX_INIT_RECORD = "agento-toolbox-init"


def _build_root() -> Path:
    """The framework's build root — the ONE the rest of Agento uses.

    An earlier version read an ``AGENTO_BUILD_DIR`` env var defaulting to
    ``/var/agento/builds``. That variable is set by nothing and defined nowhere else in
    the repo, and the default path exists in no container, so every lookup in a real
    deployment missed and the transcript-derived ``job.toolbox_mcp_connected`` stayed
    NULL. Every unit test passed
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

    def _iter_records(self, session_id: str) -> Iterator[dict]:
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
                if isinstance(record, dict):
                    yield record

    def read_toolbox_init(self, session_id: str) -> dict | None:
        """The bridge's ``agento-toolbox-init`` entry, if it was written.

        `pi.appendEntry(customType, data)` stores a custom entry keyed by
        **``customType``** — not ``name`` — directly in the session JSONL. (The stdout
        stream wraps the same entry in ``{"type":"entry_appended","entry":…}``; that shape
        is handled by ``output_parser``.)
        """
        for record in self._iter_records(session_id):
            if record.get("type") != "custom":
                continue
            if record.get("customType") != TOOLBOX_INIT_RECORD:
                continue
            data = record.get("data")
            if isinstance(data, dict):
                return data
        return None
