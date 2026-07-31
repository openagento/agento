from __future__ import annotations

import json
import logging

import pymysql

from agento.framework.bootstrap import get_module_config
from agento.framework.config_resolver import load_db_overrides, read_config_defaults
from agento.framework.core_config import config_set
from agento.framework.encryptor import get_encryptor
from agento.modules.jira.src.toolbox_client import ToolboxAPIError, ToolboxClient


class PeriodicTasksOnboarding:
    def is_complete(self, conn: pymysql.Connection) -> bool:
        overrides = load_db_overrides(conn)
        status = overrides.get("jira_periodic_tasks/jira_status")
        field = overrides.get("jira_periodic_tasks/jira_frequency_field")
        # Values are (value, encrypted) tuples; check both exist and are non-empty
        return bool(status and status[0]) and bool(field and field[0])

    def describe(self) -> str:
        return "Configure Jira status and Frequency custom field for periodic tasks"

    def run(self, conn: pymysql.Connection, config: dict, logger: logging.Logger) -> None:
        # 1. Check prerequisites
        jira_config = get_module_config("jira")
        toolbox_url = jira_config.get("toolbox_url") if isinstance(jira_config, dict) else getattr(jira_config, "toolbox_url", None)
        if not toolbox_url:
            print("  Error: core/toolbox/url not configured. Run 'agento config:set core/toolbox/url <url>' first.")
            return

        toolbox = ToolboxClient(toolbox_url)
        try:
            toolbox.jira_request("GET", "/rest/api/3/myself")
        except ToolboxAPIError as e:
            print(f"  Error: Cannot reach Jira via toolbox: {e}")
            return
        except Exception as e:
            print(f"  Error: Toolbox not reachable at {toolbox_url}: {e}")
            return

        # 2. Read DB overrides (fresh — jira onboarding may have just saved values)
        db_overrides = load_db_overrides(conn)

        # Resolve admin credentials if available (for creating statuses, fields, etc.)
        admin_auth = _resolve_admin_auth(db_overrides)
        if admin_auth:
            print("  Using admin credentials for configuration changes")

        # Get project key
        jira_projects_entry = db_overrides.get("jira/jira_projects")
        jira_projects = json.loads(jira_projects_entry[0]) if jira_projects_entry and jira_projects_entry[0] else None
        if jira_projects:
            project_key = jira_projects[0]
            print(f"  Using project: {project_key} (from jira module config)")
        else:
            project_key = input("  Jira project key: ").strip()
            if not project_key:
                print("  Error: Project key is required.")
                return

        try:
            toolbox.jira_request("GET", f"/rest/api/3/project/{project_key}")
        except ToolboxAPIError as e:
            print(f"  Error: Project '{project_key}' not found or not accessible: {e}")
            return

        # 3. Status name
        status_name = input("  Status name for periodic tasks [Periodic]: ").strip() or "Periodic"
        status_id = _find_status(toolbox, project_key, status_name)

        if status_id:
            print(f"  Found existing status '{status_name}' (id: {status_id})")
        else:
            print(f"  Status '{status_name}' not found in project. Attempting to create...")
            status_id = _create_status(toolbox, project_key, status_name, admin_auth)
            if not status_id:
                return

        # 4. Field name
        field_name = input("  Custom field name for frequency [Frequency]: ").strip() or "Frequency"
        field_id = _find_field(toolbox, field_name)

        if field_id:
            print(f"  Found existing field '{field_name}' (id: {field_id})")
        else:
            print(f"  Field '{field_name}' not found. Creating...")
            field_id = _create_field(toolbox, field_name, admin_auth)
            if not field_id:
                return

        # 5. Add dropdown options
        if not _sync_field_options(toolbox, field_id, config, logger, admin_auth):
            return

        # 6. Screen mapping (best-effort for company-managed projects)
        _try_screen_mapping(toolbox, project_key, field_id, logger, admin_auth)

        # 7. Save config to DB
        config_set(conn, "jira_periodic_tasks/jira_status", status_name)
        config_set(conn, "jira_periodic_tasks/jira_frequency_field", field_id)
        conn.commit()

        # 8. Summary
        print("\n  Onboarding complete for jira_periodic_tasks:")
        print(f"    Status: {status_name} (id: {status_id})")
        print(f"    Frequency field: {field_id}")
        print("    Config saved to core_config_data")


def _resolve_admin_auth(db_overrides: dict) -> dict | None:
    """Read admin token from DB, paired with its owner. Returns {auth_user, auth_token} or None.

    The admin token often belongs to a different account than the runtime ``jira_user`` (a site
    admin vs the bot), so pair it with ``jira_admin_user`` when set, falling back to ``jira_user``.
    """
    admin_token_entry = db_overrides.get("jira/jira_admin_token")
    if not admin_token_entry or not admin_token_entry[0]:
        return None
    admin_user_entry = db_overrides.get("jira/jira_admin_user")
    user_entry = (
        admin_user_entry if (admin_user_entry and admin_user_entry[0])
        else db_overrides.get("jira/jira_user")
    )
    if not user_entry or not user_entry[0]:
        return None
    admin_token = admin_token_entry[0]
    if admin_token_entry[1]:
        admin_token = get_encryptor().decrypt(admin_token)
    return {"auth_user": user_entry[0], "auth_token": admin_token}


def _find_status(toolbox: ToolboxClient, project_key: str, status_name: str) -> str | None:
    try:
        data = toolbox.jira_request("GET", f"/rest/api/3/project/{project_key}/statuses")
    except ToolboxAPIError:
        return None

    for issue_type in data if isinstance(data, list) else []:
        for status in issue_type.get("statuses", []):
            if status.get("name", "").lower() == status_name.lower():
                return status["id"]
    return None


def _create_status(
    toolbox: ToolboxClient, project_key: str, status_name: str,
    admin_auth: dict | None = None,
) -> str | None:
    auth_kw = admin_auth or {}
    try:
        project = toolbox.jira_request("GET", f"/rest/api/3/project/{project_key}", **auth_kw)

        payload = {
            "statuses": [{
                "name": status_name,
                "statusCategory": "TODO",
            }],
            "scope": {
                "type": "PROJECT",
                "project": {"id": project["id"]},
            },
        }

        data = toolbox.jira_request("POST", "/rest/api/3/statuses", payload, **auth_kw)
        # Response is a list of created statuses
        if isinstance(data, list) and len(data) > 0:
            status_id = data[0].get("id")
            print(f"  Created status '{status_name}' (id: {status_id})")
            return status_id
        print(f"  Error: Unexpected response from status creation: {data}")
        return None
    except ToolboxAPIError as e:
        print(f"  Error: Failed to create status '{status_name}': {e}")
        print("  You may need 'Administer Jira' permission or create the status manually.")
        return None


def _find_field(toolbox: ToolboxClient, field_name: str) -> str | None:
    try:
        fields = toolbox.jira_request("GET", "/rest/api/3/field")
    except ToolboxAPIError:
        return None

    for f in fields if isinstance(fields, list) else []:
        if (
            f.get("name", "").lower() == field_name.lower()
            and f.get("custom", False)
            and f.get("schema", {}).get("custom", "").endswith(":select")
        ):
            return f.get("id")
    return None


def _create_field(
    toolbox: ToolboxClient, field_name: str, admin_auth: dict | None = None,
) -> str | None:
    auth_kw = admin_auth or {}
    try:
        data = toolbox.jira_request("POST", "/rest/api/3/field", {
            "name": field_name,
            "type": "com.atlassian.jira.plugin.system.customfieldtypes:select",
            "searcherKey": "com.atlassian.jira.plugin.system.customfieldtypes:multiselectsearcher",
        }, **auth_kw)
        field_id = data.get("id")
        if field_id:
            print(f"  Created field '{field_name}' (id: {field_id})")
            return field_id
        print(f"  Error: Unexpected response from field creation: {data}")
        return None
    except ToolboxAPIError as e:
        print(f"  Error: Failed to create field '{field_name}': {e}")
        return None


def _sync_field_options(
    toolbox: ToolboxClient, field_id: str, config: dict, logger: logging.Logger,
    admin_auth: dict | None = None,
) -> bool:
    """Sync dropdown options. Returns True on success, False on error."""
    # Get frequency_map keys from config defaults
    frequency_map = config.get("frequency_map", {}) if isinstance(config, dict) else getattr(config, "frequency_map", {})
    if not frequency_map:
        # Fall back to reading from config.json defaults
        from pathlib import Path
        defaults = read_config_defaults(
            Path(__file__).parent.parent
        )
        frequency_map = defaults.get("frequency_map", {})

    desired_options = list(frequency_map.keys())
    if not desired_options:
        return True

    auth_kw = admin_auth or {}

    # Get field contexts
    try:
        ctx_data = toolbox.jira_request("GET", f"/rest/api/3/field/{field_id}/context", **auth_kw)
    except ToolboxAPIError as e:
        print(f"  Error: Could not get field contexts: {e}")
        return False

    contexts = ctx_data.get("values", [])
    if not contexts:
        print("  Error: No field contexts found. Cannot add options.")
        return False

    context_id = contexts[0]["id"]

    # Get existing options
    try:
        opt_data = toolbox.jira_request(
            "GET", f"/rest/api/3/field/{field_id}/context/{context_id}/option", **auth_kw
        )
    except ToolboxAPIError:
        opt_data = {}

    existing = {o["value"] for o in opt_data.get("values", [])}
    missing = [o for o in desired_options if o not in existing]

    if not missing:
        print(f"  Field options already in sync ({len(existing)} options)")
        return True

    # Add missing options
    try:
        toolbox.jira_request(
            "POST",
            f"/rest/api/3/field/{field_id}/context/{context_id}/option",
            {"options": [{"value": v} for v in missing]},
            **auth_kw,
        )
        print(f"  Added {len(missing)} field option(s): {', '.join(missing)}")
        return True
    except ToolboxAPIError as e:
        print(f"  Error: Failed to add field options: {e}")
        return False


def _try_screen_mapping(
    toolbox: ToolboxClient, project_key: str, field_id: str, logger: logging.Logger,
    admin_auth: dict | None = None,
) -> None:
    auth_kw = admin_auth or {}
    try:
        # Get project screen schemes
        project = toolbox.jira_request("GET", f"/rest/api/3/project/{project_key}", **auth_kw)
        # Team-managed (next-gen) projects manage their field layout separately, so this
        # company-managed screen-mapping path doesn't apply — the operator adds the field via the
        # project's own Fields settings in the UI.
        if project.get("style") == "next-gen":
            return

        # For company-managed: try to add field to default screen
        screens_data = toolbox.jira_request("GET", "/rest/api/3/screens", **auth_kw)
        screens = screens_data.get("values", []) if isinstance(screens_data, dict) else []
        if not screens:
            return

        default_screen = screens[0]
        screen_id = default_screen["id"]

        # Get screen tabs
        tabs = toolbox.jira_request("GET", f"/rest/api/3/screens/{screen_id}/tabs", **auth_kw)
        if not tabs:
            return

        tab_id = tabs[0]["id"] if isinstance(tabs, list) else tabs.get("values", [{}])[0].get("id")
        if not tab_id:
            return

        # Add field to screen tab
        toolbox.jira_request(
            "POST",
            f"/rest/api/3/screens/{screen_id}/tabs/{tab_id}/fields",
            {"fieldId": field_id},
            **auth_kw,
        )
        print(f"  Added field to screen '{default_screen.get('name', screen_id)}'")
    except ToolboxAPIError:
        logger.debug("Screen mapping skipped (non-critical)", exc_info=True)
    except Exception:
        logger.debug("Screen mapping skipped (non-critical)", exc_info=True)
