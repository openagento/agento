"""CLI command: exec:todo — Execute next TODO task (or specific issue)."""
from __future__ import annotations

import argparse
import sys
from contextlib import ExitStack

from agento.framework.toolbox_capability import rest_capability


class ExecTodoCommand:
    @property
    def name(self) -> str:
        return "exec:todo"

    @property
    def shortcut(self) -> str:
        return "ex:to"

    @property
    def help(self) -> str:
        return "Execute next TODO task (or specific issue)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("issue_key", nargs="?")
        parser.add_argument(
            "--agent-view", dest="agent_view", default=None,
            help="Agent view code to act as. Optional only when exactly one active "
                 "agent_view exists; there is no global fallback.",
        )

    def _resolve_agent_view(self, conn, code):
        from agento.framework.workspace import get_active_agent_views, get_agent_view_by_code

        if code:
            av = get_agent_view_by_code(conn, code)
            if av is None:
                print(f"Error: agent_view '{code}' not found", file=sys.stderr)
                sys.exit(1)
            return av

        views = get_active_agent_views(conn)
        if len(views) == 1:
            return views[0]
        # Never a global fallback: without a view there is no scope to mint a
        # capability for, and the toolbox would answer 401.
        choices = ", ".join(v.code for v in views) or "none"
        print(
            f"Error: --agent-view is required ({len(views)} active agent_views: {choices}).",
            file=sys.stderr,
        )
        sys.exit(1)

    def execute(self, args: argparse.Namespace) -> None:
        from agento.framework.bootstrap import bootstrap
        from agento.framework.channels.registry import get_channel
        from agento.framework.cli.runtime import _load_framework_config, _make_runner
        from agento.framework.config_resolver import ScopedConfigService
        from agento.framework.db import get_connection
        from agento.framework.job_models import AgentType, Job
        from agento.framework.log import get_logger
        from agento.framework.scoped_config import Scope
        from agento.framework.workflows import get_workflow_class
        from agento.framework.workflows.base import JobContext

        db_config, _, _ = _load_framework_config()
        logger = get_logger("exec-jira-todo-task", "/app/logs/exec-jira-todo-task.log", stderr=False)

        conn = get_connection(db_config)
        capability_token = None
        try:
            bootstrap(db_conn=conn)
            av = self._resolve_agent_view(conn, args.agent_view)
            logger.info("exec:todo acting as agent_view %s (id=%d)", av.code, av.id)
            jira_config = ScopedConfigService(conn, Scope.AGENT_VIEW, av.id).get_module("jira")
            if jira_config is None:
                print(
                    f"Error: could not resolve jira config for agent_view '{av.code}'",
                    file=sys.stderr,
                )
                sys.exit(1)
            # The whole run, not only `execute_job`, is inside the capability's lifetime:
            # a failure building the channel, the runner or the context would otherwise
            # leave the bearer live for the rest of its TTL.
            with ExitStack() as stack:
                if not args.issue_key:
                    # Only the discovery flow reaches the toolbox REST API; a named issue
                    # goes straight to the agent and needs no capability at all.
                    capability_token = stack.enter_context(
                        rest_capability(agent_view_id=av.id, db_config=db_config)
                    )

                channel = get_channel("jira")
                runner = _make_runner(logger)
                workflow = get_workflow_class(AgentType.TODO)(runner, logger)

                job = Job.stub(
                    type=AgentType.TODO, source="jira", reference_id=args.issue_key or None,
                )
                context = JobContext(
                    config=jira_config,
                    logger=logger,
                    update_reference_id=lambda *a: None,
                    capability_token=capability_token,
                )
                workflow.execute_job(channel, job, context)
        finally:
            conn.close()
