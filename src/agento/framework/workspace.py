"""Workspace and AgentView models — first-class entities for Phase 9 hierarchy."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

_CODE_RE = re.compile(r"[a-z][a-z0-9_]*")


def validate_code(code: str, entity: str = "code") -> None:
    """Validate a workspace or agent_view code.

    Codes must start with a lowercase letter and contain only lowercase letters,
    digits, and underscores.  Starting with ``_`` is reserved for theme/workspace
    layering scope directories and for the artifacts root's own dirs (``_threads``,
    ``_fallback``). ``fullmatch``: ``$`` would also take a code that ends in a newline.
    """
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
        raise ValueError(
            f"Invalid {entity} code {code!r}: must match [a-z][a-z0-9_]* "
            "(lowercase start, no leading underscore)"
        )


@dataclass
class Workspace:
    id: int
    code: str
    label: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: dict) -> Workspace:
        return cls(
            id=row["id"],
            code=row["code"],
            label=row["label"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class AgentView:
    id: int
    workspace_id: int
    code: str
    label: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: dict) -> AgentView:
        return cls(
            id=row["id"],
            workspace_id=row["workspace_id"],
            code=row["code"],
            label=row["label"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def get_active_agent_views(conn) -> list[AgentView]:
    """Load all active agent_views with their workspace also active."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT av.*
            FROM agent_view av
            JOIN workspace w ON w.id = av.workspace_id
            WHERE av.is_active = 1 AND w.is_active = 1
            ORDER BY av.id
            """
        )
        return [AgentView.from_row(row) for row in cur.fetchall()]


def get_workspace(conn, workspace_id: int) -> Workspace | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM workspace WHERE id = %s", (workspace_id,))
        row = cur.fetchone()
    return Workspace.from_row(row) if row else None


def get_agent_view(conn, agent_view_id: int) -> AgentView | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM agent_view WHERE id = %s", (agent_view_id,))
        row = cur.fetchone()
    return AgentView.from_row(row) if row else None


def get_agent_view_by_code(conn, code: str) -> AgentView | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM agent_view WHERE code = %s", (code,))
        row = cur.fetchone()
    return AgentView.from_row(row) if row else None
