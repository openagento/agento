"""Whether a Jira run continues the issue's previous session (`jira/resume_session`).

Every job on one issue is one thread, and by default a turn resumes the session of the
thread's last finished turn (framework/threads.py). An operator may turn that off per
agent_view: the job then stays in the issue's thread and line (the same order, the same
shared directory) and starts a fresh session (`JobClaimBeforeEvent.fresh_session`).
"""
from __future__ import annotations

from agento.framework.config_resolver import ScopedConfigService
from agento.framework.database_config import DatabaseConfig
from agento.framework.db import get_connection, pooled
from agento.framework.events import JobClaimBeforeEvent
from agento.framework.job_models import AgentType
from agento.framework.scoped_config import Scope

SOURCE = "jira"


def resume_session_on(conn, path: str, *, agent_view_id: int | None) -> bool:
    """The boolean at `path` for the view (agent_view -> workspace -> default); unset is on.

    Strict: a scope level that cannot be read raises, and the claim then defers the job,
    rather than answer with a less specific level's value.
    """
    if agent_view_id is None:
        svc = ScopedConfigService(conn, strict=True)
    else:
        svc = ScopedConfigService(conn, Scope.AGENT_VIEW, agent_view_id, strict=True)
    value = svc.get(path)
    # None only when no manifest is loaded (config.json holds the default `true`): resume,
    # the framework's behaviour with this module absent.
    return value is None or value.strip().lower() in ("1", "true", "yes")


class ResumeSessionPolicy:
    """Ask the claim for a fresh session when the module's `resume_session` is off.

    One subclass per module that owns such a field: `CONFIG_PATH` is the field and `TYPES`
    the job types of the Jira source it governs.
    """

    CONFIG_PATH = "jira/resume_session"
    TYPES: tuple[str, ...] = (AgentType.TODO.value, AgentType.FOLLOWUP.value)

    def execute(self, event: object) -> None:
        if not isinstance(event, JobClaimBeforeEvent):
            return
        # A pooled connection that only reads; the claim transaction (which holds the job
        # row) is open on another one.
        with pooled(DatabaseConfig.from_env(), get_connection) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT type, source, agent_view_id FROM job WHERE id = %s",
                            (event.job_id,))
                job = cur.fetchone()
            if job is None or job["source"] != SOURCE or job["type"] not in self.TYPES:
                return
            if not resume_session_on(conn, self.CONFIG_PATH, agent_view_id=job["agent_view_id"]):
                event.fresh_session = True
