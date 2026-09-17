"""Observers for the jira_periodic_tasks module."""
from __future__ import annotations

import logging

from .crontab import MARKER_BEGIN, CronEntry, CrontabManager

logger = logging.getLogger(__name__)


class RestoreCrontabOnSetupObserver:
    """Rebuild the ``JIRA-SYNC`` crontab block from the ``schedule`` table after
    ``setup:upgrade``.

    The cron container's entrypoint rewrites the ``agent`` crontab from scratch on
    every start (``crontab -u agent -``), dropping the dynamic
    ``JIRA-SYNC:BEGIN/END`` block. ``setup:upgrade`` runs right after but only
    restores the framework ``AGENTO`` block, so recurring Jira tasks stay missing
    until the next hourly ``jira:periodic:sync`` — a window of up to 59 minutes in
    which scheduled runs are silently skipped (and ``sync-jira-cron.log`` keeps
    reporting ``Crontab unchanged``, so nothing signals the gap).

    This observer closes that window by regenerating the block from the
    ``schedule`` table, which ``jira:periodic:sync`` already keeps current
    (``sync.py::_upsert_schedules``). It is a pure DB read: no Jira/toolbox
    roundtrip, so it cannot race toolbox readiness at container start and works
    even when Jira is unreachable. The next hourly sync reconciles against Jira
    regardless.

    When the module is disabled it is never registered (bootstrap skips disabled
    modules' observers), so the block is simply not restored — which is correct,
    since the hourly sync that would maintain it is disabled too. Any runtime error
    here is non-fatal to ``setup:upgrade`` (the dispatcher swallows observer
    exceptions; we also log our own line).
    """

    def execute(self, event) -> None:
        if getattr(event, "dry_run", False):
            return
        try:
            self._restore()
        except Exception:
            logger.warning(
                "jira_periodic_tasks: failed to restore JIRA-SYNC crontab block on "
                "setup:upgrade (non-fatal; the hourly sync will reconcile)",
                exc_info=True,
            )

    def _restore(self) -> None:
        from agento.framework.bootstrap import get_module_config
        from agento.framework.database_config import DatabaseConfig
        from agento.framework.db import get_connection
        from agento.framework.lock import FileLock, LockHeld
        from agento.framework.workspace import get_active_agent_views

        periodic_config = get_module_config("jira_periodic_tasks")
        freq_labels = _reverse_frequency_map(
            getattr(periodic_config, "frequency_map", None) or {}
        )

        conn = get_connection(DatabaseConfig.from_env())
        try:
            active_view_ids = [av.id for av in get_active_agent_views(conn)]
            entries = _load_entries(conn, freq_labels, active_view_ids)
        finally:
            conn.close()

        crontab = CrontabManager()
        had_block = MARKER_BEGIN in crontab.get_current()

        try:
            with FileLock():
                changed = crontab.apply_managed(entries)
        except LockHeld:
            # A jira:periodic:sync is already running and will (re)write the block
            # authoritatively — nothing for us to do.
            logger.debug(
                "jira_periodic_tasks: sync lock held during setup restore; skipping"
            )
            return

        if entries and not had_block:
            # We rebuilt a block that was absent while the schedule table held live
            # entries — a real gap in the recurring schedule was just recovered
            # (typically a container restart). Worth an explicit warning: the hourly
            # "Crontab unchanged" log can never surface this state.
            logger.warning(
                "jira_periodic_tasks: restored missing JIRA-SYNC crontab block with "
                "%d %s after setup:upgrade — recurring tasks were absent since the "
                "last container start; any schedule due in that gap did not run",
                len(entries),
                _plural(len(entries)),
            )
        else:
            logger.info(
                "jira_periodic_tasks: JIRA-SYNC crontab block %s on setup:upgrade "
                "(%d %s)",
                "updated" if changed else "already current",
                len(entries),
                _plural(len(entries)),
            )


def _reverse_frequency_map(frequency_map: dict[str, str]) -> dict[str, str]:
    """Map a cron expression back to its frequency label, for the crontab comment.

    ``schedule`` stores ``cron_expr`` but not the human label the hourly sync writes
    from Jira. Reverse-mapping keeps the restored comment identical to the synced one
    (first label wins on the rare duplicate expression) so a subsequent sync sees no
    diff. The label is cosmetic — an unmatched expression just falls back to blank,
    costing at most one extra rewrite on the next sync.
    """
    reversed_map: dict[str, str] = {}
    for label, expr in frequency_map.items():
        reversed_map.setdefault(expr, label)
    return reversed_map


def _load_entries(
    conn, freq_labels: dict[str, str], active_view_ids: list[int]
) -> list[CronEntry]:
    """Read enabled recurring schedules into crontab entries (DictCursor rows).

    Scoped exactly like the hourly ``jira:periodic:sync`` so the restored block
    equals what the sync would emit: per-view rows for the currently *active*
    agent_views, or global (``agent_view_id IS NULL``) rows when none is active
    (``commands/sync.py``). This deliberately excludes rows the sync would never
    put in the crontab — stale global rows left over from a single-view→multi-view
    migration (the sync's disable-sweep never touches global rows once views
    exist) and rows for a now-inactive view. Restoring those would resurrect
    obsolete schedules that could fire in the gap before the next sync removes them
    again.
    """
    with conn.cursor() as cur:
        if active_view_ids:
            placeholders = ",".join(["%s"] * len(active_view_ids))
            cur.execute(
                f"""
                SELECT s.issue_key, s.summary, s.cron_expr, av.code AS agent_view_code
                FROM schedule s
                JOIN agent_view av ON av.id = s.agent_view_id
                WHERE s.agent_type = 'cron' AND s.enabled = TRUE
                  AND s.agent_view_id IN ({placeholders})
                ORDER BY s.agent_view_id, s.issue_key
                """,
                active_view_ids,
            )
        else:
            cur.execute(
                """
                SELECT s.issue_key, s.summary, s.cron_expr, '' AS agent_view_code
                FROM schedule s
                WHERE s.agent_type = 'cron' AND s.enabled = TRUE
                  AND s.agent_view_id IS NULL
                ORDER BY s.issue_key
                """
            )
        rows = cur.fetchall()

    entries: list[CronEntry] = []
    for row in rows:
        cron_expr = row["cron_expr"] or ""
        entries.append(
            CronEntry(
                issue_key=row["issue_key"],
                summary=row["summary"] or "",
                frequency_label=freq_labels.get(cron_expr, ""),
                cron_expression=cron_expr,
                agent_view_code=row["agent_view_code"] or "",
            )
        )
    return entries


def _plural(n: int) -> str:
    return "entry" if n == 1 else "entries"
