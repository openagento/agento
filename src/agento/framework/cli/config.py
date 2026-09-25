from __future__ import annotations

import argparse
import json
import sys

from ..config_schema_options import field_options
from ..db import get_connection_or_exit
from ..scoped_config import Scope
from .runtime import _load_framework_config


def _validate_config_path(path: str, scope: str = Scope.DEFAULT) -> bool:
    """Print the refusal and return False; the rule itself lives in ``config_write``."""
    from ..config_write import ConfigWriteError, validate_config_path

    try:
        validate_config_path(path, scope)
    except ConfigWriteError as exc:
        print(exc)
        return False
    return True


def _validate_config_value(
    path: str, value: str, *, conn=None, scope: str | None = None, scope_id: int = 0,
) -> bool:
    from ..config_write import ConfigWriteError, validate_config_value

    try:
        validate_config_value(path, value, conn=conn, scope=scope, scope_id=scope_id)
    except ConfigWriteError as exc:
        print(exc)
        return False
    return True


def _config_get_exact(conn, path: str) -> None:
    """Display config value for exact path, deduplicated across scopes."""
    from ..core_config import config_get

    rows = config_get(conn, path)
    if not rows:
        print(f"Not set: {path}")
        return

    # Mask obscure values (encrypted or declared obscure in schema)
    for row in rows:
        row["display"] = "****" if row.get("obscure") else row["value"]

    # Deduplicate: if all display values are the same, show one line
    values = {r["display"] for r in rows}
    if len(values) == 1:
        print(f"  {path} = {rows[0]['display']}")
        return

    for row in rows:
        scope_tag = _format_scope_tag(conn, row["scope"], row["scope_id"])
        print(f"  {path} = {row['display']}  [{scope_tag}]")


def _config_get_tree(conn, prefix: str) -> None:
    """Display all config for a module prefix in tree view grouped by scope."""
    from collections import OrderedDict

    from ..bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
    from ..config_resolver import read_config_defaults
    from ..core_config import config_get_tree
    from ..module_loader import scan_modules

    rows = config_get_tree(conn, prefix + "/")

    # Also load config.json defaults for the module
    manifests = scan_modules(CORE_MODULES_DIR) + scan_modules(USER_MODULES_DIR)
    manifest = next((m for m in manifests if m.name == prefix.replace("-", "_")), None)
    config_defaults = read_config_defaults(manifest.path) if manifest else {}

    # Build config.json entries (tool fields)
    cfg_entries = {}
    if config_defaults.get("tools"):
        for tool_name, fields in config_defaults["tools"].items():
            for field_name, value in fields.items():
                cfg_path = f"{prefix}/tools/{tool_name}/{field_name}"
                cfg_entries[cfg_path] = value

    # Group DB rows by (scope, scope_id)
    groups: OrderedDict[tuple[str, int, str], list[dict]] = OrderedDict()

    # Ensure default group exists first
    groups[(Scope.DEFAULT, 0, "")] = []

    for row in rows:
        key = (row["scope"], row["scope_id"], row["scope_label"])
        if key not in groups:
            groups[key] = []
        groups[key].append(row)

    # Add config.json defaults to default group (if not overridden by DB)
    db_paths_default = {r["path"] for r in groups.get((Scope.DEFAULT, 0, ""), [])}
    for cfg_path, cfg_value in cfg_entries.items():
        if cfg_path not in db_paths_default:
            groups[(Scope.DEFAULT, 0, "")].append({
                "path": cfg_path, "value": str(cfg_value), "source": "config.json",
            })

    if all(len(entries) == 0 for entries in groups.values()):
        print(f"No config found for: {prefix}")
        return

    print(f"  {prefix}")
    group_list = list(groups.items())
    for i, ((scope, scope_id, scope_label), entries) in enumerate(group_list):
        if not entries:
            continue
        is_last = all(len(e) == 0 for _, e in group_list[i + 1:])

        # Scope header
        if scope == Scope.DEFAULT:
            header = Scope.DEFAULT
        elif scope_label:
            header = f"{scope}: {scope_label} (id={scope_id})"
        else:
            header = f"{scope}/{scope_id}"

        connector = "\u2514" if is_last else "\u251c"
        pipe = " " if is_last else "\u2502"
        print(f"  {connector} {header}")

        for entry in sorted(entries, key=lambda e: e["path"]):
            # Strip module prefix from path for cleaner display
            display_path = entry["path"]
            if display_path.startswith(prefix + "/"):
                display_path = display_path[len(prefix) + 1:]
            source = f"  [{entry.get('source', '')}]" if entry.get("source") else ""
            print(f"  {pipe}   {display_path} = {entry['value']}{source}")


def _format_scope_tag(conn, scope: str, scope_id: int) -> str:
    """Format a human-readable scope tag like 'default' or 'agent_view: Jira'."""
    if scope == Scope.DEFAULT:
        return Scope.DEFAULT
    if scope == Scope.WORKSPACE:
        from ..workspace import get_workspace
        ws = get_workspace(conn, scope_id)
        return f"workspace: {ws.code}" if ws else f"workspace/{scope_id}"
    if scope == Scope.AGENT_VIEW:
        from ..workspace import get_agent_view
        av = get_agent_view(conn, scope_id)
        return f"agent_view: {av.code}" if av else f"agent_view/{scope_id}"
    return f"{scope}/{scope_id}"


def _is_field_obscure(manifest, field_name: str) -> bool:
    """Check if a module config field is obscure type."""
    schema = manifest.config.get(field_name, {})
    return schema.get("type") == "obscure"


def _format_value(value: object) -> str:
    """Format a config value for display."""
    if value is None:
        return "(not set)"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _read_value_from_stdin(path: str) -> str:
    """Read a config value from stdin. Prompts only if stdin is a TTY.

    When TTY (interactive paste): strip a single trailing newline (the Enter
    pressed before Ctrl+D). When piped: return verbatim to respect pipe contents.
    """
    is_tty = sys.stdin.isatty()
    if is_tty:
        print(
            f"Paste value for {path}, then press Ctrl+D on an empty line to finish:",
            file=sys.stderr,
        )
    value = sys.stdin.read()
    if is_tty and value.endswith("\n"):
        value = value[:-1]
    return value


def _resolve_scope_from_args(conn, args) -> tuple[str, int]:
    """Resolve (scope, scope_id) from argparse args.

    Supports `--agent-view <code>` as a shortcut that looks up the id. Rejects
    conflicts between `--agent-view` and `--scope-id` / non-matching `--scope`.
    """
    agent_view_code = getattr(args, "agent_view", None)
    scope = getattr(args, "scope", Scope.DEFAULT)
    scope_id = getattr(args, "scope_id", 0)

    if agent_view_code is None:
        return scope, scope_id

    if scope not in (Scope.DEFAULT, Scope.AGENT_VIEW):
        print(
            f"Error: --agent-view cannot be combined with --scope={scope}",
            file=sys.stderr,
        )
        sys.exit(2)
    if scope_id not in (0, None):
        print("Error: --agent-view and --scope-id are mutually exclusive", file=sys.stderr)
        sys.exit(2)

    from ..workspace import get_agent_view_by_code

    av = get_agent_view_by_code(conn, agent_view_code)
    if av is None:
        print(f"Error: agent_view '{agent_view_code}' not found", file=sys.stderr)
        sys.exit(1)
    return Scope.AGENT_VIEW, av.id


class ConfigSetCommand:
    @property
    def name(self) -> str:
        return "config:set"

    @property
    def shortcut(self) -> str:
        return "co:se"

    @property
    def help(self) -> str:
        return "Set a config value in core_config_data (value from arg, pipe, or paste)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("path", help="Config path (e.g. my_app/tools/mysql_prod/pass)")
        parser.add_argument(
            "value", nargs="?", default=None,
            help="Value to set. Omit to read from stdin (paste interactively or pipe).",
        )
        parser.add_argument("--scope", default=Scope.DEFAULT,
                           help="Config scope: default, workspace, agent_view")
        parser.add_argument("--scope-id", type=int, default=0,
                           help="Scope ID (workspace or agent_view ID)")
        parser.add_argument(
            "--agent-view", dest="agent_view", default=None,
            help="Agent view code — shortcut for --scope agent_view --scope-id <lookup>",
        )

    def execute(self, args: argparse.Namespace) -> None:
        # Every rejection below exits non-zero: `config:set` is scripted (CI, the e2e
        # suite, onboarding), and a printed error with rc=0 reads as a successful write.
        if "/" not in args.path:
            print(f"Error: Invalid config path '{args.path}' — expected module/field format (e.g. jira/jira_token)")
            sys.exit(1)

        value = args.value
        if value is None:
            value = _read_value_from_stdin(args.path)

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            scope, scope_id = _resolve_scope_from_args(conn, args)
            scope_label = f" [scope={scope}, scope_id={scope_id}]" if scope != Scope.DEFAULT else ""
            # One shared write path with the admin TUI and the web admin API: validation,
            # dependent repair in the same transaction, and config_save_after.
            from ..config_write import ConfigWriteError, save_config

            try:
                encrypted, reset = save_config(
                    conn, args.path, value, scope=scope, scope_id=scope_id, allow_secret=True,
                )
            except ConfigWriteError as exc:
                print(exc)
                sys.exit(1)
            label = " (encrypted)" if encrypted else ""
            print(f"Set: {args.path}{label}{scope_label}")
            for dep_path, dep_value in reset:
                print(f"  also set: {dep_path} = {dep_value} (was invalid for {value})")
        finally:
            conn.close()


class ConfigGetCommand:
    @property
    def name(self) -> str:
        return "config:get"

    @property
    def shortcut(self) -> str:
        return "co:ge"

    @property
    def help(self) -> str:
        return "Get a config value from core_config_data"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("path", help="Config path")

    def execute(self, args: argparse.Namespace) -> None:
        path = args.path
        is_exact = "/" in path

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            if is_exact:
                _config_get_exact(conn, path)
            else:
                _config_get_tree(conn, path)
        finally:
            conn.close()


class ConfigListCommand:
    @property
    def name(self) -> str:
        return "config:list"

    @property
    def shortcut(self) -> str:
        return "co:li"

    @property
    def help(self) -> str:
        return "List config values"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("prefix", nargs="?", default="", help="Filter by path prefix (e.g. module name)")

    def execute(self, args: argparse.Namespace) -> None:
        from ..bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
        from ..config_resolver import (
            load_db_overrides,
            read_config_defaults,
            resolve_module_config_with_sources,
            resolve_tool_field,
        )
        from ..core_config import config_list
        from ..module_loader import scan_modules

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            prefix = args.prefix or ""

            manifests = scan_modules(CORE_MODULES_DIR) + scan_modules(USER_MODULES_DIR)
            db_overrides = load_db_overrides(conn)
            shown_module_config = False

            for m in manifests:
                if prefix and not m.name.startswith(prefix):
                    continue
                if m.config:
                    config_defaults = read_config_defaults(m.path)
                    resolved = resolve_module_config_with_sources(
                        m, config_defaults, db_overrides
                    )
                    for field_name, rv in resolved.items():
                        display = "****" if rv.source == "db" and _is_field_obscure(m, field_name) else _format_value(rv.value)
                        print(f"  {m.name}/{field_name} = {display}  [{rv.source}]")
                        shown_module_config = True

                for tool in m.tools:
                    if prefix and not f"{m.name}/tools/{tool['name']}".startswith(prefix):
                        continue
                    config_defaults = read_config_defaults(m.path)
                    for field_name, field_schema in tool.get("fields", {}).items():
                        rv = resolve_tool_field(
                            m.name, tool["name"], field_name,
                            field_schema, config_defaults, db_overrides,
                        )
                        is_obscure = field_schema.get("type") == "obscure"
                        display = "****" if is_obscure and rv.value else _format_value(rv.value)
                        print(f"  {m.name}/tools/{tool['name']}/{field_name} = {display}  [{rv.source}]")
                        shown_module_config = True

            if not shown_module_config:
                entries = config_list(conn, prefix=prefix)
                if not entries:
                    print("No config entries found.")
                    return
                for entry in entries:
                    enc = " [encrypted]" if entry["encrypted"] else ""
                    scope_label = f"{entry['scope']}/{entry['scope_id']}"
                    print(f"  {entry['path']} = {entry['value']}{enc}  [{scope_label}]")
        finally:
            conn.close()


class ConfigSchemaCommand:
    @property
    def name(self) -> str:
        return "config:schema"

    @property
    def shortcut(self) -> str:
        return "co:sc"

    @property
    def help(self) -> str:
        return "Show config field definitions from system.json"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("module", nargs="?", default=None, help="Module name (omit for all)")
        parser.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON")

    def execute(self, args: argparse.Namespace) -> None:
        from ..bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
        from ..module_loader import scan_modules

        manifests = scan_modules(CORE_MODULES_DIR) + scan_modules(USER_MODULES_DIR)

        if args.module:
            manifests = [m for m in manifests if m.name == args.module]
            if not manifests:
                print(f"Error: Module '{args.module}' not found.")
                return

        if args.as_json:
            data = []
            for m in manifests:
                if not m.config and not m.tools:
                    continue
                tools_schema = {}
                for tool in m.tools:
                    fields = tool.get("fields", {})
                    if fields:
                        tools_schema[tool["name"]] = fields
                data.append({
                    "module": m.name,
                    "fields": m.config,
                    "tools": tools_schema,
                })
            print(json.dumps(data, indent=2))
            return

        from ..config_schema import allowed_scopes

        unreachable: list[str] = []
        found = False
        for m in manifests:
            if not m.config and not m.tools:
                continue
            found = True
            print(f"Module: {m.name}")
            for field_name, field_schema in m.config.items():
                ftype = field_schema.get("type", "string")
                label = field_schema.get("label", "")
                description = field_schema.get("description", "")
                print(f"  {field_name:<20s}{ftype:<10s}{label}")
                options = field_options(field_schema)
                if options and isinstance(options, list):
                    vals = ", ".join(o["value"] for o in options if isinstance(o, dict) and "value" in o)
                    print(f"  {'':20s}{'':10s}options: {vals}")
                if description:
                    print(f"  {'':20s}{'':10s}info: {description}")
                if isinstance(field_schema, dict) and not allowed_scopes(field_schema):
                    unreachable.append(f"{m.name}/{field_name}")
            for tool in m.tools:
                for field_name, field_schema in tool.get("fields", {}).items():
                    ftype = field_schema.get("type", "string")
                    label = field_schema.get("label", "")
                    description = field_schema.get("description", "")
                    print(f"  tools/{tool['name']}/{field_name:<20s}{ftype:<10s}{label}")
                    if description:
                        print(f"  {'':20s}{'':10s}info: {description}")
                    if isinstance(field_schema, dict) and not allowed_scopes(field_schema):
                        unreachable.append(f"{m.name}/tools/{tool['name']}/{field_name}")

        if not found:
            print("No config schema found.")

        if unreachable:
            print()
            print("Warning: the following fields have all showIn* flags set to false and are unreachable:")
            for path in unreachable:
                print(f"  - {path}")


class ConfigResolveCommand:
    @property
    def name(self) -> str:
        return "config:resolve"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Resolve effective config values with source info"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("module", help="Module name")
        parser.add_argument("--scope", default=Scope.DEFAULT,
                           choices=[Scope.DEFAULT, Scope.WORKSPACE, Scope.AGENT_VIEW],
                           help="Config scope")
        parser.add_argument("--scope-id", type=int, default=0,
                           help="Scope ID (workspace or agent_view ID)")
        parser.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON")

    def execute(self, args: argparse.Namespace) -> None:
        from ..bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
        from ..config_resolver import ScopedConfigService, read_config_defaults
        from ..module_loader import scan_modules

        manifests = scan_modules(CORE_MODULES_DIR) + scan_modules(USER_MODULES_DIR)
        manifest = next((m for m in manifests if m.name == args.module), None)
        if manifest is None:
            print(f"Error: Module '{args.module}' not found.")
            return

        if not manifest.config and not manifest.tools:
            print(f"Module '{args.module}' has no config fields.")
            return

        scope = args.scope
        scope_id = getattr(args, "scope_id", 0)

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            config_defaults = read_config_defaults(manifest.path)

            # Single resolver owns the ENV -> scoped DB -> config.json fallback +
            # db:inherited detection.
            svc = ScopedConfigService(conn, scope, scope_id)

            fields_output = []

            for field_name, field_schema in manifest.config.items():
                rv, inherited = svc.resolve_field_with_source(
                    manifest.name, field_name, field_schema, config_defaults,
                )
                source = "db:inherited" if inherited else rv.source

                is_obscure = field_schema.get("type") == "obscure"
                display = "****" if is_obscure and rv.value is not None else _format_value(rv.value)

                fields_output.append({
                    "path": f"{manifest.name}/{field_name}",
                    "field": field_name,
                    "value": rv.value,
                    "display_value": display,
                    "source": source,
                    "type": field_schema.get("type", "string"),
                    "label": field_schema.get("label", ""),
                    "obscure": is_obscure,
                    "inherited": inherited,
                })

            for tool in manifest.tools:
                for field_name, field_schema in tool.get("fields", {}).items():
                    rv, inherited = svc.resolve_tool_field_with_source(
                        manifest.name, tool["name"], field_name,
                        field_schema, config_defaults,
                    )
                    source = "db:inherited" if inherited else rv.source

                    is_obscure = field_schema.get("type") == "obscure"
                    display = "****" if is_obscure and rv.value is not None else _format_value(rv.value)

                    fields_output.append({
                        "path": f"{manifest.name}/tools/{tool['name']}/{field_name}",
                        "field": field_name,
                        "value": rv.value,
                        "display_value": display,
                        "source": source,
                        "type": field_schema.get("type", "string"),
                        "label": field_schema.get("label", ""),
                        "obscure": is_obscure,
                        "inherited": inherited,
                    })

            if args.as_json:
                print(json.dumps({
                    "module": manifest.name,
                    "scope": scope,
                    "scope_id": scope_id,
                    "fields": fields_output,
                }, indent=2))
            else:
                print(f"Module: {manifest.name} (scope={scope})")
                for f in fields_output:
                    source_tag = f["source"] if f["source"] != "none" else "-"
                    print(f"  {f['field']:<20s}= {f['display_value']:<25s}[{source_tag}]")
        finally:
            conn.close()


class ConfigRemoveCommand:
    @property
    def name(self) -> str:
        return "config:remove"

    @property
    def shortcut(self) -> str:
        return "co:re"

    @property
    def help(self) -> str:
        return "Remove a config value from DB"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("path", help="Config path to remove")
        parser.add_argument("--scope", default=Scope.DEFAULT,
                          help="Config scope: default, workspace, agent_view")
        parser.add_argument("--scope-id", type=int, default=0,
                          help="Scope ID (workspace or agent_view ID)")
        parser.add_argument(
            "--agent-view", dest="agent_view", default=None,
            help="Agent view code — shortcut for --scope agent_view --scope-id <lookup>",
        )

    def execute(self, args: argparse.Namespace) -> None:
        from ..core_config import config_delete

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            scope, scope_id = _resolve_scope_from_args(conn, args)
            scope_label = f" [scope={scope}, scope_id={scope_id}]" if scope != Scope.DEFAULT else ""
            deleted = config_delete(conn, args.path, scope=scope, scope_id=scope_id)
            conn.commit()
            if deleted:
                print(f"Removed: {args.path}{scope_label}")
            else:
                print(f"Not found: {args.path}{scope_label}")
        finally:
            conn.close()


