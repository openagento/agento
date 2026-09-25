"""Observers for the agent_view module."""
from __future__ import annotations

import logging

from agento.framework.database_config import DatabaseConfig
from agento.framework.db import get_connection
from agento.framework.scoped_config import build_scoped_overrides
from agento.framework.workspace import get_agent_view

from .instruction_writer import write_instruction_files

logger = logging.getLogger(__name__)


class PopulateInstructionsObserver:
    """Write AGENTS.md and SOUL.md into the artifacts directory before CLI execution.

    Observes ``agent_view_run_start_before`` — fires after config files are
    generated but before the CLI subprocess starts.
    """

    def execute(self, event) -> None:
        if not event.artifacts_dir or event.agent_view_id is None:
            return

        # Skip if pre-built workspace already has instruction files
        from pathlib import Path
        if Path(event.artifacts_dir, "AGENTS.md").exists():
            return

        try:
            conn = get_connection(DatabaseConfig.from_env())
            try:
                agent_view = get_agent_view(conn, event.agent_view_id)
                if agent_view is None:
                    logger.warning("agent_view %d not found, skipping instructions", event.agent_view_id)
                    return

                overrides = build_scoped_overrides(
                    conn,
                    agent_view_id=agent_view.id,
                    workspace_id=agent_view.workspace_id,
                )
            finally:
                conn.close()

            write_instruction_files(event.artifacts_dir, overrides)

        except Exception:
            logger.exception("Failed to populate instruction files (non-fatal)")
