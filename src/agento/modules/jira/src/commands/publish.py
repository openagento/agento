"""CLI command: publish — Publish Jira jobs to the queue."""
from __future__ import annotations

import argparse
import sys
from contextlib import closing

from agento.framework.job_models import JobRequester, RequesterTrust
from agento.framework.log import get_logger
from agento.framework.toolbox_capability import rest_capability
from agento.modules.jira.src.channel import JiraPublisher, publish_cron
from agento.modules.jira.src.task_list import TaskListBuilder
from agento.modules.jira.src.toolbox_client import ToolboxClient


def _todo_requester(builder, issue) -> JobRequester | None:
    change, complete = builder.get_status_change(issue.key, issue.status)  # change None -> no authoritative actor
    author = (change or {}).get("author") or {}
    if change and author.get("accountId"):                        # `change and` keeps the type-checker happy
        return JobRequester(
            key=f"jira:{author['accountId']}",
            email=author.get("emailAddress"),                     # JobRequester normalizes (strip+lower)
            trust=RequesterTrust.ACCOUNT,
            meta={"basis": "status_change", "issue_key": issue.key, "status": issue.status,
                  "display_name": author.get("displayName"),
                  "changelog_id": change.get("id"), "changed_at": change.get("created")},
        )
    if issue.reporter_account_id:  # weaker attribution (reporter != status-changer) - flagged via basis
        # honest audit: 3 distinct reasons we fell back to the reporter. We only reach this
        # branch when the status_change actor was unusable, so `change` being truthy here means
        # a transition WAS found but its author had no accountId (unattributable actor).
        if change:
            fallback_reason = "status_change_actor_unavailable"
        elif complete:                                            # full scan, no transition into current status
            fallback_reason = "no_status_transition"
        else:                                                     # changelog unreadable (fetch error / page cap)
            fallback_reason = "changelog_unavailable"
        return JobRequester(
            key=f"jira:{issue.reporter_account_id}",
            email=issue.reporter_email,                           # JobRequester normalizes
            trust=RequesterTrust.ACCOUNT,
            meta={"basis": "reporter", "issue_key": issue.key, "status": issue.status,
                  "display_name": issue.reporter,
                  "fallback_reason": fallback_reason},
        )
    return None


def _load_configs():
    """Load framework + jira module config via bootstrap."""
    from agento.framework.bootstrap import bootstrap, get_module_config
    from agento.framework.cli.runtime import _load_framework_config
    from agento.framework.db import get_connection

    db_config, _, _ = _load_framework_config()
    conn = get_connection(db_config)
    try:
        bootstrap(db_conn=conn)
    finally:
        conn.close()
    return db_config, get_module_config("jira")


def _get_connection_and_bootstrap():
    """Bootstrap and return (db_config, conn). Caller must close conn."""
    from agento.framework.bootstrap import bootstrap
    from agento.framework.cli.runtime import _load_framework_config
    from agento.framework.db import get_connection

    db_config, _, _ = _load_framework_config()
    conn = get_connection(db_config)
    try:
        bootstrap(db_conn=conn)
    except Exception:
        conn.close()
        raise
    return db_config, conn


_publisher = JiraPublisher()


class PublishCommand:
    @property
    def name(self) -> str:
        return "publish"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Publish a job to the queue"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("kind", choices=["jira-cron", "jira-todo", "jira-mention"])
        parser.add_argument("issue_key", nargs="?")
        parser.add_argument("--agent-view", dest="agent_view", default=None)

    def execute(self, args: argparse.Namespace) -> None:
        logger = get_logger("publisher", "/app/logs/publisher.log", stderr=False)
        kind = args.kind

        if kind == "jira-cron":
            self._execute_cron(args, logger)
            return

        # For todo (no issue_key) and mention: iterate active agent_views
        db_config, conn = _get_connection_and_bootstrap()
        try:
            self._execute_per_agent_view(kind, args, db_config, conn, logger)
        finally:
            conn.close()

    def _execute_cron(self, args, logger):
        """Cron: specific issue_key. Prefer explicit --agent-view, else single-view heuristic, else routing."""
        if not args.issue_key:
            logger.error("issue_key required for jira-cron")
            sys.exit(1)

        db_config, conn = _get_connection_and_bootstrap()
        try:
            if args.agent_view:
                from agento.framework.agent_view_runtime import resolve_publish_priority
                from agento.framework.workspace import get_agent_view_by_code

                av = get_agent_view_by_code(conn, args.agent_view)
                if av is None:
                    logger.error("Unknown agent_view code %r", args.agent_view)
                    sys.exit(1)
                _publisher.publish_cron(
                    db_config, args.issue_key, logger,
                    agent_view_id=av.id, priority=resolve_publish_priority(conn, av.id),
                )
                return

            agent_view_id, priority = self._resolve_single_agent_view(conn, logger)
            if agent_view_id is not None:
                _publisher.publish_cron(
                    db_config, args.issue_key, logger,
                    agent_view_id=agent_view_id, priority=priority,
                )
            else:
                # Multiple or zero enabled agent_views — fall back to routing
                publish_cron(db_config, args.issue_key, logger)
        finally:
            conn.close()

    def _execute_per_agent_view(self, kind, args, db_config, conn, logger):
        """Iterate active agent_views, resolve scoped config for each, publish."""
        from agento.framework.agent_view_runtime import resolve_publish_priority
        from agento.framework.config_resolver import ScopedConfigService
        from agento.framework.scoped_config import Scope
        from agento.framework.workspace import get_active_agent_views

        agent_views = get_active_agent_views(conn)

        if not agent_views:
            # No global fallback: the toolbox scopes every REST call to a capability, and
            # a capability requires a view. A viewless publish has no correct scope, so it
            # fails loudly rather than running unauthenticated.
            logger.error(
                "no active agent_view — jira publishing is per-view. Configure one for "
                "this workspace (see docs/architecture/workspace.md) and re-run."
            )
            sys.exit(1)

        for av in agent_views:
            try:
                jira_config = ScopedConfigService(conn, Scope.AGENT_VIEW, av.id).get_module("jira")
                if jira_config is None:
                    logger.warning("Could not resolve jira config for agent_view %s, skipping", av.code)
                    continue
                if not jira_config.enabled:
                    logger.debug("Jira disabled for agent_view %s, skipping", av.code)
                    continue

                priority = resolve_publish_priority(conn, av.id)
                logger.info("Publishing %s for agent_view %s (id=%d)", kind, av.code, av.id)

                # One short-lived capability per view, minted and revoked around this
                # view's calls — the toolbox derives the view from it.
                with rest_capability(agent_view_id=av.id, db_config=db_config) as capability_token:
                    if kind == "jira-todo":
                        self._publish_todo_for_agent_view(
                            args, db_config, jira_config, av.id, priority, logger,
                            capability_token=capability_token,
                        )
                    elif kind == "jira-mention":
                        count = _publisher.publish_mentions(
                            jira_config, logger, db_config=db_config,
                            agent_view_id=av.id, priority=priority,
                            capability_token=capability_token,
                        )
                        logger.info("Published %d mention jobs for agent_view %s", count, av.code)
            except Exception:
                logger.exception(
                    "Publish %s failed for agent_view %s (id=%d) — continuing with remaining views",
                    kind, av.code, av.id,
                )
                continue

    def _publish_todo_for_agent_view(
        self, args, db_config, jira_config, agent_view_id, priority, logger,
        *, capability_token: str,
    ):
        """Publish all TODO tasks for a specific agent_view."""
        if args.issue_key:
            _publisher.publish_todo(
                db_config, args.issue_key, logger=logger,
                agent_view_id=agent_view_id, priority=priority,
            )
            return
        ai_user = jira_config.jira_assignee or jira_config.user
        if not ai_user:
            logger.warning("jira_assignee/user not set for this agent_view, skipping")
            return
        with closing(
            ToolboxClient(jira_config.toolbox_url, capability_token=capability_token)
        ) as toolbox:
            builder = TaskListBuilder(toolbox, jira_config, ai_user, logger, agent_view_id=agent_view_id)
            tasks = builder.get_todo_tasks()
            if not tasks:
                logger.debug("No TODO tasks, skipping")
                return
            published = 0
            for task in tasks:
                inserted = _publisher.publish_todo(
                    db_config, reference_id=task.issue.key,
                    updated=task.issue.updated, logger=logger,
                    agent_view_id=agent_view_id, priority=priority,
                    requester=_todo_requester(builder, task.issue),
                )
                if inserted:
                    published += 1
            logger.info("Published %d of %d todo jobs for agent_view %d", published, len(tasks), agent_view_id)

    def _resolve_single_agent_view(self, conn, logger):
        """If exactly 1 jira-enabled agent_view exists, return (id, priority). Else (None, 50)."""
        from agento.framework.agent_view_runtime import resolve_publish_priority
        from agento.framework.config_resolver import ScopedConfigService
        from agento.framework.scoped_config import Scope
        from agento.framework.workspace import get_active_agent_views

        agent_views = get_active_agent_views(conn)
        enabled = []
        for av in agent_views:
            jira_config = ScopedConfigService(conn, Scope.AGENT_VIEW, av.id).get_module("jira")
            if jira_config is not None and jira_config.enabled:
                enabled.append(av)

        if len(enabled) == 1:
            av = enabled[0]
            priority = resolve_publish_priority(conn, av.id)
            return av.id, priority

        return None, 50
