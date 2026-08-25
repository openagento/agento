"""CLI command: jira:periodic:sync — Jira recurring tasks to crontab."""
from __future__ import annotations

import argparse
import sys
from contextlib import closing

from agento.framework.toolbox_capability import rest_capability
from agento.modules.jira.src.toolbox_client import ToolboxClient
from agento.modules.jira_periodic_tasks.src.crontab import CronEntry, CrontabManager
from agento.modules.jira_periodic_tasks.src.sync import JiraCronSync


class SyncCommand:
    @property
    def name(self) -> str:
        return "jira:periodic:sync"

    @property
    def shortcut(self) -> str:
        return "ji:pe:sy"

    @property
    def help(self) -> str:
        return "Sync Jira recurring tasks to crontab"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true")

    def execute(self, args: argparse.Namespace) -> None:
        from agento.framework.bootstrap import bootstrap, get_module_config
        from agento.framework.cli.runtime import _load_framework_config
        from agento.framework.config_resolver import ScopedConfigService
        from agento.framework.db import get_connection
        from agento.framework.lock import FileLock, LockHeld
        from agento.framework.log import get_logger
        from agento.framework.scoped_config import Scope
        from agento.framework.workspace import get_active_agent_views

        db_config, _, _ = _load_framework_config()
        logger = get_logger("sync-jira-cron", "/app/logs/sync-jira-cron.log", stderr=False)
        crontab = CrontabManager()

        conn = get_connection(db_config)
        try:
            bootstrap(db_conn=conn)
            periodic_config = get_module_config("jira_periodic_tasks")
            agent_views = get_active_agent_views(conn)

            try:
                lock = FileLock()
                with lock:
                    all_entries: list[CronEntry] = []

                    if not agent_views:
                        # No global fallback: a viewless sync has no scope to mint a
                        # toolbox capability for, so every REST call would be refused.
                        logger.error(
                            "no active agent_view — jira_periodic_tasks sync is per-view. "
                            "Configure one (see docs/architecture/workspace.md) and re-run."
                        )
                        sys.exit(1)
                    else:
                        for av in agent_views:
                            try:
                                jira_config = ScopedConfigService(
                                    conn, Scope.AGENT_VIEW, av.id
                                ).get_module("jira")
                                if jira_config is None:
                                    logger.warning(
                                        "No jira config for agent_view %s, skipping", av.code
                                    )
                                    continue
                                if not jira_config.enabled:
                                    logger.debug(
                                        "Jira disabled for agent_view %s, skipping", av.code
                                    )
                                    continue
                                with rest_capability(
                                    agent_view_id=av.id, db_config=db_config
                                ) as capability_token, closing(
                                    ToolboxClient(
                                        jira_config.toolbox_url,
                                        capability_token=capability_token,
                                    )
                                ) as toolbox:
                                    syncer = JiraCronSync(
                                        jira_config, periodic_config, toolbox,
                                        crontab, logger, db_config=db_config,
                                        agent_view_id=av.id, agent_view_code=av.code,
                                    )
                                    all_entries.extend(
                                        syncer.sync_view(dry_run=args.dry_run)
                                    )
                            except Exception:
                                logger.exception(
                                    "Sync failed for agent_view %s — continuing", av.code
                                )
                                continue

                    changed = crontab.apply_managed(all_entries, dry_run=args.dry_run)
                    logger.info(
                        "Crontab %s (%d total entries across %d view(s))",
                        "updated" if changed else "unchanged",
                        len(all_entries),
                        max(len(agent_views), 1),
                    )
            except LockHeld as e:
                logger.warning(f"Another sync is running ({e}). Exiting.")
        finally:
            conn.close()
