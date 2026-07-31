"""Module validation — checks module structure and manifest integrity."""
from __future__ import annotations

import json
import re
from pathlib import Path

REQUIRED_MANIFEST_FIELDS = {"name", "version", "description"}
# Full-access adapter types must carry their capability in the tool NAME. Tool enablement is
# keyed by name (`tools/<name>/is_enabled`) and records nothing about capability, so promoting
# an already-enabled read-only tool by editing its type in place would inherit the old grant.
#
# The mapping is enforced in BOTH directions, and only the pair makes promotion a rename:
#   1. a full-access type MUST use its suffix, and
#   2. the suffix is RESERVED — no other type may use it, so a read-only tool cannot squat the
#      name first and be escalated later by an edit to its `type` alone.
# Mirrored at runtime by the toolbox mysql adapter. Note this binds a NAME to a capability, not
# a grant to a capability: an is_enabled row left behind by a deleted full-access tool of the
# same name would still apply if that name is reused.
FULL_ACCESS_TOOL_NAME_SUFFIXES = {"mysql_root": "_root"}
RESERVED_TOOL_NAME_SUFFIXES = frozenset(FULL_ACCESS_TOOL_NAME_SUFFIXES.values())
VALID_FIELD_TYPES = {"string", "integer", "boolean", "obscure", "select", "multiselect", "json", "textarea"}
# Canonical identity-type form (fits ingress_identity.identity_type VARCHAR(32)); rejects
# whitespace / control chars / wrong shape that a bare "non-empty ≤32" check would let through.
_IDENTITY_TYPE_FORM = re.compile(r"[a-z][a-z0-9_]{0,31}")


def _resolve_class_path(module_dir: Path, class_path: str) -> bool:
    """Check if a di.json/events.json class path resolves to an existing .py file.

    Class path format: 'src.commands.hello.HelloCommand'
    -> check if {module_dir}/src/commands/hello.py exists.
    """
    parts = class_path.rsplit(".", 1)
    if len(parts) < 2:
        return False
    module_path = parts[0]
    file_path = module_dir / (module_path.replace(".", "/") + ".py")
    return file_path.is_file()


def validate_module(module_dir: Path) -> list[str]:
    """Validate a module directory structure and manifests.

    Returns list of error messages (empty = valid).
    """
    errors, _ = _validate_module(module_dir)
    return errors


def _validate_module(module_dir: Path) -> tuple[list[str], dict | None]:
    """Validate a module and return (errors, parsed_manifest).

    The manifest is returned for cross-validation in validate_all(),
    avoiding a second read of module.json.
    """
    errors: list[str] = []
    module_dir = Path(module_dir)

    # module.json
    manifest_path = module_dir / "module.json"
    if not manifest_path.is_file():
        errors.append("module.json not found")
        return errors, None

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        errors.append(f"module.json: invalid JSON — {e}")
        return errors, None

    if not isinstance(manifest, dict):
        errors.append("module.json: must be a JSON object")
        return errors, None

    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in manifest:
            errors.append(f"module.json: missing required field '{field}'")

    # Validate sequence
    sequence = manifest.get("sequence", [])
    if not isinstance(sequence, list):
        errors.append("module.json: 'sequence' must be an array")
    else:
        for entry in sequence:
            if not isinstance(entry, str):
                errors.append(f"module.json: sequence entries must be strings, got {type(entry).__name__}")

    # Validate tools
    tools = manifest.get("tools", [])
    if not isinstance(tools, list):
        errors.append("module.json: 'tools' must be an array")
    else:
        for i, tool in enumerate(tools):
            if not isinstance(tool, dict):
                errors.append(f"module.json: tools[{i}] must be an object")
                continue
            # 'toolset' is required so every tool declares its admin-UI group
            # explicitly (the Tools screen falls back to the module name at
            # runtime, but validation makes the grouping intentional).
            for tf in ("type", "name", "description", "toolset"):
                if tf not in tool:
                    errors.append(f"module.json: tools[{i}] missing '{tf}'")

            tool_name = str(tool.get("name", ""))
            suffix = FULL_ACCESS_TOOL_NAME_SUFFIXES.get(tool.get("type"))
            if suffix and not tool_name.endswith(suffix):
                errors.append(
                    f"module.json: tools[{i}] type '{tool['type']}' grants full read/write, so its "
                    f"name '{tool_name}' must end in '{suffix}' — capability must be visible "
                    "in the tool name (enablement is keyed by name, so renaming forces fresh consent)"
                )
            elif not suffix:
                squatted = next((s for s in RESERVED_TOOL_NAME_SUFFIXES if tool_name.endswith(s)), None)
                if squatted:
                    errors.append(
                        f"module.json: tools[{i}] name '{tool_name}' ends in '{squatted}', which is "
                        f"reserved for full-access tool types ({', '.join(sorted(FULL_ACCESS_TOOL_NAME_SUFFIXES))}) "
                        f"— type '{tool.get('type')}' must not use it, otherwise the tool could later be "
                        "escalated in place by editing only its type, keeping its is_enabled grant"
                    )

    # di.json
    di_path = module_dir / "di.json"
    if di_path.is_file():
        try:
            di = json.loads(di_path.read_text())
        except json.JSONDecodeError as e:
            errors.append(f"di.json: invalid JSON — {e}")
            di = None

        if di is not None and isinstance(di, dict):
            for section in ("channels", "workflows", "commands"):
                for entry in di.get(section, []):
                    if isinstance(entry, dict) and "class" in entry and not _resolve_class_path(module_dir, entry["class"]):
                        errors.append(
                            f"di.json: {section} class '{entry['class']}' does not resolve to a .py file"
                        )
            if "regex_identity_types" in di:
                regex_types = di["regex_identity_types"]
                # Key presence (not `is not None`): an explicit `null` is a malformed declaration
                # and must be rejected as "not an array", not silently accepted.
                if not isinstance(regex_types, list):
                    errors.append("di.json: 'regex_identity_types' must be an array")
                else:
                    for i, t in enumerate(regex_types):
                        if not isinstance(t, str) or not _IDENTITY_TYPE_FORM.fullmatch(t):
                            errors.append(
                                f"di.json: regex_identity_types[{i}] must match ^[a-z][a-z0-9_]{{0,31}}$"
                            )

    # events.json
    events_path = module_dir / "events.json"
    if events_path.is_file():
        try:
            events = json.loads(events_path.read_text())
        except json.JSONDecodeError as e:
            errors.append(f"events.json: invalid JSON — {e}")
            events = None

        if events is not None and isinstance(events, dict):
            # events.json format: {event_name: [observer_dicts]} or {"observers": [observer_dicts]}
            for _event_name, observer_list in events.items():
                if not isinstance(observer_list, list):
                    continue
                for observer in observer_list:
                    if isinstance(observer, dict) and "class" in observer and not _resolve_class_path(module_dir, observer["class"]):
                        errors.append(
                            f"events.json: observer class '{observer['class']}' does not resolve to a .py file"
                        )

    # config.json
    config_path = module_dir / "config.json"
    if config_path.is_file():
        try:
            json.loads(config_path.read_text())
        except json.JSONDecodeError as e:
            errors.append(f"config.json: invalid JSON — {e}")

    # system.json
    system_path = module_dir / "system.json"
    if system_path.is_file():
        try:
            system = json.loads(system_path.read_text())
        except json.JSONDecodeError as e:
            errors.append(f"system.json: invalid JSON — {e}")
            system = None

        if system is not None and isinstance(system, dict):
            for field_name, field_def in system.items():
                if not isinstance(field_def, dict):
                    continue
                field_type = field_def.get("type")
                if field_type and field_type not in VALID_FIELD_TYPES:
                    errors.append(
                        f"system.json: field '{field_name}' has invalid type '{field_type}'"
                    )
                # Validate options for select/multiselect fields
                is_select = field_type in ("select", "multiselect")
                has_options = "options" in field_def
                if is_select and not has_options:
                    errors.append(
                        f"system.json: field '{field_name}' (type '{field_type}') requires 'options'"
                    )
                if has_options and not is_select:
                    errors.append(
                        f"system.json: field '{field_name}' has 'options' but type is '{field_type}' (only select/multiselect support options)"
                    )
                if has_options:
                    options = field_def["options"]
                    if not isinstance(options, list):
                        errors.append(
                            f"system.json: field '{field_name}' options must be an array"
                        )
                    else:
                        for i, opt in enumerate(options):
                            if not isinstance(opt, dict):
                                errors.append(
                                    f"system.json: field '{field_name}' options[{i}] must be an object"
                                )
                            elif "value" not in opt or "label" not in opt:
                                errors.append(
                                    f"system.json: field '{field_name}' options[{i}] must have 'value' and 'label'"
                                )

    return errors, manifest


def validate_all(core_dir: Path, user_dir: Path) -> dict[str, list[str]]:
    """Validate all modules in core and user directories.

    Returns dict of {module_name: [errors]} for modules with errors.
    Includes cross-module sequence validation (unresolvable dependencies).
    """
    results: dict[str, list[str]] = {}
    all_modules: dict[str, dict] = {}  # name -> manifest

    for scan_dir in (core_dir, user_dir):
        if not scan_dir.is_dir():
            continue
        for entry in sorted(scan_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
                continue
            errors, manifest = _validate_module(entry)
            if errors:
                results[entry.name] = errors
            if manifest is not None:
                all_modules[manifest.get("name", entry.name)] = manifest

    # Cross-validate sequence references
    available_names = set(all_modules.keys())
    for name, manifest in all_modules.items():
        for dep in manifest.get("sequence", []):
            if dep not in available_names:
                results.setdefault(name, []).append(
                    f"module.json: sequence dependency '{dep}' not found on disk"
                )

    return results
