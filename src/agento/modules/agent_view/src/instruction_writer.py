"""Write per-job instruction files (AGENTS.md, SOUL.md, CLAUDE.md) from scoped config.

Fallback chain: DB (agent_view → workspace → global) → workspace file on disk.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from agento.framework.workspace_paths import THEME_DIR

logger = logging.getLogger(__name__)

CLAUDE_MD_CONTENT = "# Instructions\n\nPlease read and follow [AGENTS.md](AGENTS.md).\n"

_FILES = {
    "agent_view/instructions/agents_md": "AGENTS.md",
    "agent_view/instructions/soul_md": "SOUL.md",
}


def write_instruction_files(
    artifacts_dir: str | Path,
    scoped_overrides: dict[str, tuple[str, bool]],
    workspace_dir: str | Path | None = None,
) -> None:
    """Write AGENTS.md, SOUL.md, and CLAUDE.md into an artifacts directory.

    For each file: use DB config value if present, otherwise copy from workspace_dir.
    CLAUDE.md only POINTS at AGENTS.md, so it is written only when AGENTS.md is there to
    point at — see the note at the write below.
    """
    rd = Path(artifacts_dir)
    wd = Path(workspace_dir) if workspace_dir else Path(THEME_DIR)

    for config_path, filename in _FILES.items():
        entry = scoped_overrides.get(config_path)
        if entry is not None:
            value, _encrypted = entry
            if value:
                (rd / filename).write_text(value)
                logger.debug("Wrote %s from scoped config", filename)
                continue

        # Fallback: copy from workspace directory
        workspace_file = wd / filename
        if workspace_file.is_file():
            shutil.copy2(workspace_file, rd / filename)
            logger.debug("Copied %s from workspace", filename)

    # CLAUDE.md is a POINTER, not instructions: its whole content is "read AGENTS.md".
    # Written unconditionally it sent the agent to a file that need not exist — an
    # agent_view with no `instructions/agents_md` and an empty theme got a dead end as its
    # only entry point, and then had nothing but one-line tool descriptions to work from.
    # A stale one is removed for the same reason: it would outlive the AGENTS.md it names.
    claude_md = rd / "CLAUDE.md"
    if (rd / "AGENTS.md").is_file():
        claude_md.write_text(CLAUDE_MD_CONTENT)
    else:
        claude_md.unlink(missing_ok=True)
