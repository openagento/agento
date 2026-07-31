"""CLI command: jira:periodic:configure — multi-project Jira status/field setup + check.

Operator-run **setup** command (a peer of the module onboarding + ``setup:upgrade``), not the
credential-free runtime publisher path. It deliberately decrypts ``jira/jira_admin_token`` via
``_resolve_admin_auth`` and forwards it to the toolbox exactly as onboarding does — an accepted
exception to toolbox-only secret confinement (plan C6). Every *other* config read uses per-path
``.get()`` (never ``get_module``) so the non-admin ``jira_token`` is never decrypted here (C5).
"""
from __future__ import annotations

import argparse


class ConfigureCommand:
    @property
    def name(self) -> str:
        return "jira:periodic:configure"

    @property
    def shortcut(self) -> str:
        return "ji:pe:co"

    @property
    def help(self) -> str:
        return "Configure/verify Jira periodic status + Frequency field (setup; uses admin token)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--check", action="store_true", help="Read-only report; no Jira mutation")
        parser.add_argument(
            "--project", action="append", dest="project",
            help="Jira project key (repeatable); default = union across active agent_views",
        )
        parser.add_argument(
            "projects", nargs="*",
            help="Jira project key(s), positional; unioned with --project",
        )

    def execute(self, args: argparse.Namespace) -> None:
        import json
        import sys

        from agento.framework.bootstrap import bootstrap, get_module_config
        from agento.framework.cli.runtime import _load_framework_config
        from agento.framework.config_resolver import ScopedConfigService, load_db_overrides
        from agento.framework.core_config import config_set
        from agento.framework.db import get_connection
        from agento.framework.log import get_logger
        from agento.framework.scoped_config import Scope
        from agento.framework.workspace import get_active_agent_views
        from agento.modules.jira.src.toolbox_client import ToolboxClient
        from agento.modules.jira_periodic_tasks.src.configure import (
            JiraPeriodicConfigurer,
            render_report,
        )
        from agento.modules.jira_periodic_tasks.src.onboarding import _resolve_admin_auth

        db_config, _, _ = _load_framework_config()
        logger = get_logger("configure-jira-periodic", "/app/logs/configure-jira-periodic.log", stderr=False)
        check = bool(args.check)

        conn = get_connection(db_config)
        try:
            bootstrap(db_conn=conn)
            periodic = get_module_config("jira_periodic_tasks")

            # Non-secret jira reads via per-path .get() (C5) — never get_module("jira").
            cfg = ScopedConfigService(conn)  # default scope
            toolbox_url = cfg.get("core/toolbox/url")
            if not toolbox_url:
                print("Error: core/toolbox/url not configured. Run 'agento config:set core/toolbox/url <url>'.")
                sys.exit(1)

            toolbox = ToolboxClient(toolbox_url)
            try:
                toolbox.jira_request("GET", "/rest/api/3/myself")
            except Exception as e:
                print(f"Error: cannot reach Jira via toolbox at {toolbox_url}: {e}")
                sys.exit(1)

            admin_auth = _resolve_admin_auth(load_db_overrides(conn))
            if not check and admin_auth is None:
                print("Error: apply requires an admin token. Set 'jira/jira_admin_token' and its owner "
                      "'jira/jira_admin_user' (falls back to 'jira/jira_user') "
                      "(or use --check for a read-only report).")
                sys.exit(1)

            projects = self._derive_projects(
                args, conn, cfg, logger, json, Scope, ScopedConfigService, get_active_agent_views
            )
            if not projects:
                print("configure: no Jira projects to configure (pass --project or set jira/jira_projects).")
                sys.exit(0)

            status_name = (getattr(periodic, "jira_status", "") or "") or "Periodic"
            stored_field_id = getattr(periodic, "jira_frequency_field", "") or None
            frequency_map = getattr(periodic, "frequency_map", {}) or {}

            configurer = JiraPeriodicConfigurer(
                toolbox,
                projects=projects,
                status_name=status_name,
                field_id=stored_field_id,
                frequency_map=frequency_map,
                admin_auth=admin_auth,
                logger=logger,
                check=check,
            )
            report = configurer.run()
            print(render_report(report, check=check))

            # Persist (apply only, guarded — F5): never corrupt config on a failed run;
            # write only non-empty, changed values.
            if not check and not report.failed and report.resolved_field_id:
                changed = False
                if report.resolved_field_id != stored_field_id:
                    config_set(conn, "jira_periodic_tasks/jira_frequency_field", report.resolved_field_id)
                    changed = True
                stored_status = getattr(periodic, "jira_status", "") or ""
                if status_name and status_name != stored_status:
                    config_set(conn, "jira_periodic_tasks/jira_status", status_name)
                    changed = True
                if changed:
                    conn.commit()

            if check:
                sys.exit(1 if (report.inconsistent or report.incomplete) else 0)
            sys.exit(1 if report.failed else 0)
        finally:
            conn.close()

    @staticmethod
    def _derive_projects(
        args, conn, cfg, logger, json, Scope, ScopedConfigService, get_active_agent_views,
    ) -> list[str]:
        explicit = list(args.project or []) + list(getattr(args, "projects", None) or [])
        if explicit:
            return list(dict.fromkeys(explicit))

        def _parse(raw):
            if not raw:
                return []
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                return None
            return [str(p) for p in parsed] if isinstance(parsed, list) else []

        projects: list[str] = []
        views = get_active_agent_views(conn)
        if views:
            for av in views:
                svc = ScopedConfigService(conn, Scope.AGENT_VIEW, av.id)
                enabled = svc.get("jira/enabled")
                if enabled in (False, 0, "0", "false", "False"):
                    continue
                parsed = _parse(svc.get("jira/jira_projects"))
                if parsed is None:
                    logger.warning("Cannot parse jira/jira_projects for agent_view %s — skipping", av.code)
                    continue
                projects.extend(parsed)
        else:
            parsed = _parse(cfg.get("jira/jira_projects"))
            projects = parsed or []
        return list(dict.fromkeys(projects))
