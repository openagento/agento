from __future__ import annotations

import argparse
import json
import sys

from ..config_schema_options import field_options
from ..db import get_connection_or_exit
from ..scoped_config import Scope
from .runtime import _load_framework_config


def _validate_config_path(path: str, scope: str = Scope.DEFAULT) -> bool:
    """Validate config path against module schema. Returns False if invalid.

    Checks field existence plus Magento-style scope restriction flags
    (`showInDefault` / `showInWorkspace` / `showInAgentView`) declared in
    system.json (module fields) or module.json tool fields.
    """
    from ..config_schema import allowed_scopes, is_scope_allowed
    from ..core_config import _find_module_dir, _parse_config_path

    parsed = _parse_config_path(path)
    if parsed is None:
        print(f"Error: Invalid config path '{path}'.")
        return False
    module_name, tool_name, field_name = parsed

    module_dir = _find_module_dir(module_name)
    if module_dir is None:
        print(f"Error: Module '{module_name}' not found.")
        return False

    # Tool config paths (module/tools/tool_name/field)
    if tool_name is not None:
        field_def = _load_tool_field_schema(module_dir, tool_name, field_name)
        if field_def is None:
            return True
        if not is_scope_allowed(field_def, scope):
            scopes = ", ".join(allowed_scopes(field_def)) or "none"
            print(f"Error: Field '{field_name}' cannot be set at scope '{scope}' "
                  f"(allowed: {scopes})")
            return False
        return True

    # Module config paths (module/field) — validate against system.json.
    # field_name may contain '/' (slash-keyed schema fields).
    system_path = module_dir / "system.json"
    if system_path.exists():
        try:
            system = json.loads(system_path.read_text())
        except (ValueError, OSError):
            return True
        if field_name not in system:
            known = ", ".join(sorted(system.keys()))
            print(f"Error: Field '{field_name}' not found in {module_name}/system.json")
            print(f"  Available fields: {known}")
            return False
        field_def = system[field_name]
        if isinstance(field_def, dict) and not is_scope_allowed(field_def, scope):
            scopes = ", ".join(allowed_scopes(field_def)) or "none"
            print(f"Error: Field '{field_name}' cannot be set at scope '{scope}' "
                  f"(allowed: {scopes})")
            return False

    return True


def _load_tool_field_schema(module_dir, tool_name: str, field_name: str) -> dict | None:
    """Return schema dict for a tool field, or None if not discoverable."""
    manifest_path = module_dir / "module.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except (ValueError, OSError):
        return None
    for tool in manifest.get("tools", []):
        if tool.get("name") == tool_name:
            field = tool.get("fields", {}).get(field_name)
            return field if isinstance(field, dict) else None
    return None


def _effective_depends_on_value(
    conn, depends_on: str, *, scope: str, scope_id: int,
) -> str | None:
    """The value a dependent select is narrowed by, resolved AT THE TARGET SCOPE.

    Must honour the full precedence chain (ENV > agent_view > workspace > default >
    config.json), not just a raw DB row at this scope: a view inheriting its harness from
    the default scope, or one set only via ``CONFIG__AGENT_VIEW__HARNESS``, still has an
    effective harness that must narrow the provider list.
    """
    from ..config_resolver import read_config_defaults
    from ..core_config import _find_module_dir
    from ..scoped_config import ORIGIN_ABSENT, Scope, resolve_with_origin

    module, _, field = depends_on.partition("/")
    module_dir = _find_module_dir(module)
    defaults = read_config_defaults(module_dir) if module_dir is not None else {}
    json_value = (defaults or {}).get(field) if field else None

    value, origin = resolve_with_origin(
        conn, depends_on,
        agent_view_id=scope_id if scope == Scope.AGENT_VIEW else None,
        workspace_id=scope_id if scope == Scope.WORKSPACE else None,
        config_json_value=str(json_value) if json_value is not None else None,
    )
    return None if origin == ORIGIN_ABSENT else value


def _validate_config_value(
    path: str, value: str, *, conn=None, scope: str | None = None, scope_id: int = 0,
) -> bool:
    """Validate config value against system.json options for select fields. Returns False if invalid."""
    from ..core_config import _find_module_dir, _parse_config_path

    parsed = _parse_config_path(path)
    if parsed is None:
        return True
    module_name, tool_name, field_name = parsed
    if tool_name is not None:
        # Tool-field option validation is not handled here today; keep behaviour.
        return True

    module_dir = _find_module_dir(module_name)
    if module_dir is None:
        return True

    system_path = module_dir / "system.json"
    if not system_path.exists():
        return True

    import json as _json
    try:
        system = _json.loads(system_path.read_text())
    except (ValueError, OSError):
        return True

    field_def = system.get(field_name)
    if not isinstance(field_def, dict):
        return True

    if _is_private_key_field(field_name, field_def):
        return _validate_private_key(field_name, value)

    field_type = field_def.get("type")
    if field_type not in ("select", "multiselect"):
        return True

    # For a dynamic select (harness / provider) the allowed values come from the
    # agent_harnesses declarations on disk — config:set has no bootstrap() available.
    # A DEPENDENT select (provider depends on harness) must be narrowed to the harness
    # actually in effect at this scope; validating against the union of every harness's
    # providers would accept `(claude, openai)` and only fail at runtime.
    depends_on = field_def.get("depends_on")
    depends_value = None
    if depends_on and conn is not None and scope is not None:
        depends_value = _effective_depends_on_value(
            conn, depends_on, scope=scope, scope_id=scope_id,
        )
    options = field_options(field_def, depends_on_value=depends_value)
    allowed = [opt["value"] for opt in options if isinstance(opt, dict) and "value" in opt]
    if value not in allowed:
        print(f"Error: Invalid value '{value}' for {field_type} field '{field_name}'")
        print(f"  Allowed values: {', '.join(allowed)}")
        return False

    return True


def _is_private_key_field(field_name: str, field_def: dict) -> bool:
    """An ``obscure`` field whose name ends in ``ssh_private_key``.

    Name-suffix matching, not a hardcoded ``agent_view/…`` path, so the
    framework stays module-agnostic and a third-party module that follows the
    same naming gets the same protection.
    """
    return (
        field_def.get("type") == "obscure"
        and field_name.rsplit("/", 1)[-1] == "ssh_private_key"
    )


def _validate_private_key(field_name: str, value: str) -> bool:
    """Reject a private-key value that cannot parse.

    A corrupted interactive paste once stored a 36-byte value (the BEGIN header
    alone), which `identity:show` happily fingerprinted and `workspace_build`
    materialized — four silent builds of `Permission denied (publickey)`.
    Nothing legitimate fails this check: `config:set … < id_rsa` always yields a
    parsable key. Never echoes the value.
    """
    from ..ssh_keys import EncryptedKeyError, derive_public_key

    if not value.strip():
        return True  # clearing the field stays possible
    try:
        derive_public_key(value)
    except EncryptedKeyError:
        print(
            f"Error: value for '{field_name}' is a passphrase-protected private "
            f"key. The agent runs unattended and cannot unlock it — store an "
            f"unencrypted key."
        )
        return False
    except ValueError as e:
        print(f"Error: value for '{field_name}' does not parse as an SSH private key: {e}")
        print("  Set it from a file rather than an interactive paste:")
        print("    agento config:set <path> --agent-view <code> < id_rsa")
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
        from ..event_manager import get_event_manager
        from ..events import ConfigSavedEvent

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
            if not _validate_config_path(args.path, scope):
                sys.exit(1)
            if not _validate_config_value(
                args.path, value, conn=conn, scope=scope, scope_id=scope_id,
            ):
                sys.exit(1)

            scope_label = f" [scope={scope}, scope_id={scope_id}]" if scope != Scope.DEFAULT else ""
            # One shared operation for the write + any dependent repair, so `config:set`
            # and the admin TUI cannot drift apart on it. Same transaction, so a broken
            # (harness, provider) pair is never observable.
            from ..config_dependents import set_config_with_dependents

            encrypted, reset = set_config_with_dependents(
                conn, args.path, value, scope=scope, scope_id=scope_id
            )
            conn.commit()
            get_event_manager().dispatch(
                "config_save_after",
                ConfigSavedEvent(path=args.path, encrypted=encrypted),
            )
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


