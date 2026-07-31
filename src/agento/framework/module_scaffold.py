"""Module scaffolding — creates new user module directory structure."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .module_validator import FULL_ACCESS_TOOL_NAME_SUFFIXES, RESERVED_TOOL_NAME_SUFFIXES

# 'mysql' (read-only) and 'mysql_root' (full read/write) declare identical connection fields —
# the declared tool TYPE is the capability, not a config field.
_MYSQL_FIELDS = {
    "host": {"type": "string", "label": "Host"},
    "port": {"type": "integer", "label": "Port"},
    "user": {"type": "string", "label": "User"},
    "pass": {"type": "obscure", "label": "Password"},
    "database": {"type": "string", "label": "Database"},
    "client_connection_pool_max_per_tool": {"type": "integer", "label": "Maximum client connections"},
}

# Standard tool field templates by type
TOOL_FIELD_TEMPLATES = {
    "mysql": _MYSQL_FIELDS,
    "mysql_root": _MYSQL_FIELDS,
    "mssql": {
        "host": {"type": "string", "label": "Host"},
        "port": {"type": "integer", "label": "Port"},
        "user": {"type": "string", "label": "User"},
        "pass": {"type": "obscure", "label": "Password"},
        "database": {"type": "string", "label": "Database"},
        "client_connection_pool_max_per_tool": {"type": "integer", "label": "Maximum client connections"},
    },
    "opensearch": {
        "host": {"type": "string", "label": "Host"},
        "port": {"type": "integer", "label": "Port"},
        "user": {"type": "string", "label": "User"},
        "pass": {"type": "obscure", "label": "Password"},
        "index": {"type": "string", "label": "Index"},
    },
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _parse_tool(spec: str, module_name: str) -> dict:
    """Parse tool spec 'type:name:description' into a tool dict.

    'toolset' defaults to the module name — it is required by module:validate (which
    setup:upgrade runs before applying anything) and groups the tool in the admin TUI.
    """
    parts = spec.split(":", 2)
    if len(parts) < 3:
        raise ValueError(f"Invalid tool spec '{spec}'. Expected format: type:name:description")
    tool_type, tool_name, tool_desc = parts

    suffix = FULL_ACCESS_TOOL_NAME_SUFFIXES.get(tool_type)
    if suffix and not tool_name.endswith(suffix):
        raise ValueError(
            f"Tool '{tool_name}' has type '{tool_type}', which grants full read/write, so its "
            f"name must end in '{suffix}' (e.g. '{tool_name}{suffix}'). Capability belongs in the "
            "tool name: enablement is keyed by name, so a rename forces fresh operator consent."
        )
    if not suffix:
        squatted = next((s for s in RESERVED_TOOL_NAME_SUFFIXES if tool_name.endswith(s)), None)
        if squatted:
            raise ValueError(
                f"Tool name '{tool_name}' ends in '{squatted}', which is reserved for full-access "
                f"tool types ({', '.join(sorted(FULL_ACCESS_TOOL_NAME_SUFFIXES))}). Type '{tool_type}' "
                "must not use it — otherwise the tool could later be escalated in place by editing "
                "only its type, keeping its is_enabled grant."
            )

    fields = TOOL_FIELD_TEMPLATES.get(tool_type, {})
    return {
        "type": tool_type,
        "name": tool_name,
        "description": tool_desc,
        "toolset": module_name,
        "fields": fields,
    }


def scaffold_module(
    name: str,
    base_dir: Path,
    description: str = "",
    tools: list[str] | None = None,
) -> Path:
    """Create a new module directory with standard structure.

    Args:
        name: Module name (lowercase, alphanumeric + hyphens).
        base_dir: Parent directory (e.g. app/code/).
        description: Module description.
        tools: Tool specs in 'type:name:description' format.

    Returns:
        Path to the created module directory.
    """
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid module name '{name}'. Must be lowercase, start with a letter, "
            "and contain only letters, digits, and hyphens."
        )

    module_dir = base_dir / name
    if module_dir.exists():
        raise ValueError(f"Directory already exists: {module_dir}")

    parsed_tools = [_parse_tool(t, name) for t in (tools or [])]

    # Build config.json with tool field placeholders
    config: dict = {}
    if parsed_tools:
        config["tools"] = {}
        for tool in parsed_tools:
            config["tools"][tool["name"]] = {
                field_name: "" for field_name in tool["fields"]
            }

    # Create directory structure
    module_dir.mkdir(parents=True)
    (module_dir / "src").mkdir()
    (module_dir / "src" / "__init__.py").touch()
    (module_dir / "knowledge").mkdir()

    # module.json
    manifest = {
        "name": name,
        "version": "1.0.0",
        "description": description or f"{name} module",
        "tools": parsed_tools,
        "log_servers": [],
    }
    (module_dir / "module.json").write_text(json.dumps(manifest, indent=2) + "\n")

    # config.json
    (module_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n")

    # Companion files
    (module_dir / "di.json").write_text(
        json.dumps({"channels": [], "workflows": [], "commands": []}, indent=2) + "\n"
    )
    (module_dir / "events.json").write_text(
        json.dumps({"observers": []}, indent=2) + "\n"
    )
    (module_dir / "data_patch.json").write_text(
        json.dumps({"patches": []}, indent=2) + "\n"
    )
    (module_dir / "cron.json").write_text(
        json.dumps({"jobs": []}, indent=2) + "\n"
    )

    # knowledge/README.md
    (module_dir / "knowledge" / "README.md").write_text(
        f"# {name}\n\nAdd module-specific knowledge and documentation here.\n"
    )

    return module_dir
