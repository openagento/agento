from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from agento.framework.db import get_connection
from agento.framework.schedule import engine
from agento.framework.schedule.tick import default_timezone, min_interval_minutes
from agento.modules.jira.src.toolbox_client import ToolboxClient

SOURCE = "jira_periodic"


@dataclass
class CronEntry:
    """One recurring Jira issue, on its way into the ``schedule`` table as a ``jira_periodic``
    row. ``scheduler:tick`` fires it (docs/architecture/schedules.md)."""

    issue_key: str
    summary: str
    frequency_label: str
    rrule: str
    agent_view_code: str = ""


class JiraCronSync:

    def __init__(
        self,
        jira_config: object,
        periodic_config: object,
        toolbox: ToolboxClient,
        logger: logging.Logger,
        *,
        db_config: object | None = None,
        agent_view_id: int | None = None,
        agent_view_code: str = "",
        rules: dict[tuple, str | None] | None = None,
    ):
        self.jira_config = jira_config
        self.periodic_config = periodic_config
        self.toolbox = toolbox
        self.logger = logger
        self.db_config = db_config
        self.agent_view_id = agent_view_id
        self.agent_view_code = agent_view_code
        # (map value, start, zone) -> validated RRULE (None: refused). The command shares one
        # dict across its views, so each map value is validated (a child process) once per run.
        # Keyed by the value, not the label: a scoped map may give one label other rules.
        self.rules = {} if rules is None else rules

    def build_jql(self) -> str:
        jql = f'{self.jira_config.jira_project_jql} AND status = "{self.periodic_config.jira_status}"'
        account_id = getattr(self.jira_config, "jira_assignee_account_id", "")
        if account_id:
            jql += f' AND assignee = "{account_id}"'
        elif self.jira_config.jira_assignee:
            jql += f' AND assignee = "{self.jira_config.jira_assignee}"'
        return jql

    def resolve_frequency(self, freq_value: str) -> str | None:
        return self.periodic_config.frequency_map.get(freq_value)

    def parse_issues(self, response: dict) -> list[CronEntry]:
        entries: list[CronEntry] = []
        freq_field = self.periodic_config.jira_frequency_field

        for issue in response.get("issues", []):
            key = issue["key"]
            summary = issue.get("fields", {}).get("summary", "")
            freq_obj = issue.get("fields", {}).get(freq_field)

            if freq_obj is None:
                self.logger.warning(f"Issue {key} has no frequency set. Skipping.")
                continue

            freq_value = freq_obj.get("value") if isinstance(freq_obj, dict) else None
            if not freq_value:
                self.logger.warning(f"Issue {key} has no frequency value. Skipping.")
                continue

            rrule = self.resolve_frequency(freq_value)
            if not rrule:
                self.logger.warning(f"Issue {key} has unknown frequency '{freq_value}'. Skipping.")
                continue

            entries.append(CronEntry(
                issue_key=key,
                summary=summary,
                frequency_label=freq_value,
                rrule=rrule,
                agent_view_code=self.agent_view_code,
            ))

        return entries

    def sync_view(self, dry_run: bool = False) -> list[CronEntry]:
        view_tag = f"agent_view={self.agent_view_code}" if self.agent_view_code else "global"
        self.logger.debug(
            f"Starting Jira->cron sync ({view_tag}, projects={self.jira_config.jira_projects}, "
            f"status={self.periodic_config.jira_status})"
        )

        response = self.toolbox.jira_search(
            jql=self.build_jql(),
            fields=["key", "summary", self.periodic_config.jira_frequency_field],
            max_results=50,
        )

        issues = response.get("issues", [])
        self.logger.debug(
            f"[{view_tag}] Found {len(issues)} issues in Jira with status '{self.periodic_config.jira_status}'."
        )

        entries = self.parse_issues(response)
        self.logger.debug(f"[{view_tag}] Generated {len(entries)} schedule entries.")

        schedules_synced = 0
        if not dry_run:
            schedules_synced = self._upsert_schedules(entries)

        parts = [
            f"{len(issues)} issues",
            f"{len(entries)} entries",
        ]
        if not dry_run:
            parts.append(f"{schedules_synced} schedules")

        prefix = f"Sync OK [{view_tag}]" + (" [DRY RUN]" if dry_run else "")
        self.logger.info(f"{prefix}: {', '.join(parts)}")
        return entries

    def _checked_rule(self, conn, label: str, starts_at: datetime, tz: str) -> str | None:
        """The map value of ``label`` as a validated RRULE, or None (logged once per run)."""
        value = self.periodic_config.frequency_map.get(label, "")
        key = (value, starts_at, tz)
        if key in self.rules:
            return self.rules[key]
        rule = None
        try:
            rule = engine.normalize(value)
        except engine.InvalidSchedule:
            # Only an ENV override can still hold cron after the upgrade patch (AG-67 No cron compat).
            self.logger.error(f"frequency_map entry '{label}' is not an RRULE; cron is no longer accepted (AG-67)")
        if rule is not None:
            try:
                engine.validate(rule, starts_at, tz, min_interval_minutes=min_interval_minutes(conn),
                                now=datetime.now(UTC))
            except engine.InvalidSchedule as exc:
                self.logger.error(f"frequency_map entry '{label}' is refused: {exc.code}")
                rule = None
        self.rules[key] = rule
        return rule

    def _upsert_schedules(self, entries: list[CronEntry]) -> int:
        """Sync this view's ``jira_periodic`` rows with the current Jira entries. Returns the count synced.

        A changed rule resets ``next_fire_at`` and ``anchor_local`` (the tick computes them again);
        so does re-enabling a row, so a fire missed while it was off is skipped. ``starts_at`` and
        ``timezone`` are written on insert only. The resets come before ``rrule = VALUES(rrule)``:
        MySQL applies the assignments left to right.
        """
        if self.agent_view_id is None:
            self.logger.error("jira_periodic_tasks sync needs an agent_view; no schedule written")
            return 0
        try:
            conn = get_connection(self.db_config or self.jira_config)
        except Exception:
            self.logger.warning("Cannot connect to MySQL for schedules upsert, skipping.")
            return 0

        try:
            tz = default_timezone(conn)
            starts_at = datetime.now(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
            rules = {e.frequency_label: self._checked_rule(conn, e.frequency_label, starts_at, tz) for e in entries}
            kept = [e for e in entries if rules[e.frequency_label]]
            with conn.cursor() as cur:
                for entry in kept:
                    cur.execute(
                        """
                        INSERT INTO schedule (source, source_ref, title, agent_view_id, owner_agent_view_id,
                                              rrule, starts_at, timezone, enabled)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
                        ON DUPLICATE KEY UPDATE
                            next_fire_at = IF(rrule <=> VALUES(rrule) AND enabled, next_fire_at, NULL),
                            anchor_local = IF(rrule <=> VALUES(rrule), anchor_local, NULL),
                            last_error = IF(rrule <=> VALUES(rrule) AND enabled, last_error, NULL),
                            title = VALUES(title),
                            rrule = VALUES(rrule),
                            enabled = 1,
                            updated_at = NOW()
                        """,
                        (SOURCE, entry.issue_key, entry.summary, self.agent_view_id, self.agent_view_id,
                         rules[entry.frequency_label], starts_at, tz),
                    )
                keys = [e.issue_key for e in kept]
                not_kept = f" AND source_ref NOT IN ({','.join(['%s'] * len(keys))})" if keys else ""
                cur.execute(
                    "UPDATE schedule SET enabled = 0, updated_at = NOW() "
                    f"WHERE agent_view_id = %s AND source = %s{not_kept}",
                    (self.agent_view_id, SOURCE, *keys),
                )
            conn.commit()
            self.logger.debug(f"Schedules table synced ({len(kept)} entries).")
            return len(kept)
        except Exception:
            conn.rollback()
            self.logger.exception("Failed to upsert schedules table.")
            return 0
        finally:
            conn.close()
