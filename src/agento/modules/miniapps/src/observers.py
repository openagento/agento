"""Observers for the miniapps module."""
from __future__ import annotations

from agento.framework.database_config import DatabaseConfig
from agento.framework.db import get_connection


class DropActivationsOnArtifactDelete:
    """``artifact_delete_after``: the approvals of a deleted artifact go with it.

    Hygiene, not the boundary: a launch refuses an approval whose pinned content is gone.
    """

    def execute(self, event) -> None:
        conn = get_connection(DatabaseConfig.from_env())
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM miniapp_activation WHERE artifact_code = %s", (event.artifact_code,))
            conn.commit()
        finally:
            conn.close()
