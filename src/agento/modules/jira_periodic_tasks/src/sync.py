from __future__ import annotations

import logging
from dataclasses import dataclass

from agento.framework.db import get_connection
from agento.modules.jira.src.toolbox_client import ToolboxClient


@dataclass
class CronEntry:
    """One recurring Jira issue, on its way into the ``schedule`` table.

    The root crontab renderer reads that table; nothing here builds a crontab line.
    """

    issue_key: str
    summary: str
    frequency_label: str
    cron_expression: str
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
    ):
        self.jira_config = jira_config
        self.periodic_config = periodic_config
        self.toolbox = toolbox
        self.logger = logger
        self.db_config = db_config
        self.agent_view_id = agent_view_id
        self.agent_view_code = agent_view_code

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

            cron_expr = self.resolve_frequency(freq_value)
            if not cron_expr:
                self.logger.warning(f"Issue {key} has unknown frequency '{freq_value}'. Skipping.")
                continue

            entries.append(CronEntry(
                issue_key=key,
                summary=summary,
                frequency_label=freq_value,
                cron_expression=cron_expr,
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
        self.logger.debug(f"[{view_tag}] Generated {len(entries)} cron entries.")

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

    def _upsert_schedules(self, entries: list[CronEntry]) -> int:
        """Sync schedules table with current Jira entries. Returns count synced."""
        try:
            conn = get_connection(self.db_config or self.jira_config)
        except Exception:
            self.logger.warning("Cannot connect to MySQL for schedules upsert, skipping.")
            return 0

        try:
            with conn.cursor() as cur:
                for entry in entries:
                    cur.execute(
                        """
                        INSERT INTO schedule (agent_view_id, issue_key, summary, agent_type, cron_expr, enabled)
                        VALUES (%s, %s, %s, 'cron', %s, TRUE)
                        ON DUPLICATE KEY UPDATE
                            summary = VALUES(summary),
                            cron_expr = VALUES(cron_expr),
                            enabled = TRUE,
                            updated_at = NOW()
                        """,
                        (self.agent_view_id, entry.issue_key, entry.summary, entry.cron_expression),
                    )
                if entries:
                    keys = [e.issue_key for e in entries]
                    placeholders = ",".join(["%s"] * len(keys))
                    if self.agent_view_id is None:
                        cur.execute(
                            f"UPDATE schedule SET enabled = FALSE, updated_at = NOW() "
                            f"WHERE agent_view_id IS NULL AND issue_key NOT IN ({placeholders})",
                            keys,
                        )
                    else:
                        cur.execute(
                            f"UPDATE schedule SET enabled = FALSE, updated_at = NOW() "
                            f"WHERE agent_view_id = %s AND issue_key NOT IN ({placeholders})",
                            [self.agent_view_id, *keys],
                        )
                else:
                    if self.agent_view_id is None:
                        cur.execute(
                            "UPDATE schedule SET enabled = FALSE, updated_at = NOW() "
                            "WHERE agent_view_id IS NULL"
                        )
                    else:
                        cur.execute(
                            "UPDATE schedule SET enabled = FALSE, updated_at = NOW() "
                            "WHERE agent_view_id = %s",
                            (self.agent_view_id,),
                        )
            conn.commit()
            self.logger.debug(f"Schedules table synced ({len(entries)} entries).")
            return len(entries)
        except Exception:
            conn.rollback()
            self.logger.exception("Failed to upsert schedules table.")
            return 0
        finally:
            conn.close()
