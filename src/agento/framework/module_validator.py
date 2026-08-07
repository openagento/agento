"""Module validation — checks module structure and manifest integrity."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
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
# A tool registered by `server.tool('<name>', …)` in a module's toolbox JS but absent from
# module.json `tools[]` is invisible on the admin Tools screen and in `tool:list` (both
# enumerate manifests only), so its `tools/<name>/is_enabled` key can never be flipped there.
# Only string-literal names are statically knowable; names computed at runtime (upstream MCP
# passthrough) are covered by the runtime drift WARN in registerTools.
_TOOL_NAME_FORM = re.compile(r"[a-z][a-z0-9_]*")
_TOOL_ENABLED_KEY = re.compile(r"tools/([a-z][a-z0-9_]*)/is_enabled")
# Canonical identity-type form (fits ingress_identity.identity_type VARCHAR(32)); rejects
# whitespace / control chars / wrong shape that a bare "non-empty ≤32" check would let through.
_IDENTITY_TYPE_FORM = re.compile(r"[a-z][a-z0-9_]{0,31}")


def _skip_js_string(src: str, i: int) -> int:
    """Index just past the string or template literal that starts at ``i``."""
    quote = src[i]
    i += 1
    n = len(src)
    while i < n:
        if src[i] == "\\":
            i += 2
            continue
        if src[i] == quote:
            return i + 1
        i += 1
    return n


def _skip_ws_and_comments(src: str, i: int) -> int:
    """Index of the next character that is neither whitespace nor a comment."""
    n = len(src)
    while i < n:
        if src[i] in " \t\r\n":
            i += 1
        elif src.startswith("//", i):
            nl = src.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif src.startswith("/*", i):
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            return i
    return n


def _match_server_tool_call(src: str, i: int) -> int | None:
    """If a real ``server.tool(`` call starts at ``i``, the index just past ``(``.

    Requires an identifier boundary before ``server`` so ``mockserver.tool(`` does
    not match, while ``this.server.tool(`` still does. Tolerates whitespace and
    comments between ``tool`` and ``(``.
    """
    if not src.startswith("server", i):
        return None
    # `#` guards a private field: `this.#server.tool(...)` is not the framework's server.
    if i > 0 and (src[i - 1].isalnum() or src[i - 1] in "_$#"):
        return None
    j = _skip_ws_and_comments(src, i + len("server"))
    if j >= len(src) or src[j] != ".":
        return None
    j = _skip_ws_and_comments(src, j + 1)
    if not src.startswith("tool", j):
        return None
    j += len("tool")
    if j < len(src) and (src[j].isalnum() or src[j] in "_$"):
        return None   # e.g. `server.tools(` / `server.toolFoo(`
    j = _skip_ws_and_comments(src, j)
    if j >= len(src) or src[j] != "(":
        return None
    return j + 1


def _scan_server_tool_literals(src: str) -> list[str]:
    """Literal first arguments of ``server.tool(`` calls, best-effort.

    Skips comments and string/template literals, requires an identifier boundary before
    ``server``, and accepts the quoted argument only when ``,`` or ``)`` follows.

    On any other ``/`` it stops scanning that LINE. Deciding whether a ``/`` is division or the
    start of a regex literal is a full lexical-goal problem — ASI, brace grammar, postfix
    operators and the enclosing construct all feed it — so no token heuristic settles it, and
    three successive attempts here were each defeated by valid JavaScript. Since this feeds a
    FATAL ``setup:upgrade`` error, the only acceptable failure direction is a miss: giving up on
    the line cannot invent a tool, and cannot wrongly abort somebody's upgrade.

    Consequences, both deliberate: a registration sharing a line with a ``/`` is not reported, and
    neither are dynamic names (``server.tool(name, …)``, a template literal, a concatenation). The
    exact check is ``toolbox/tests/tool-declaration.test.js``, which executes ``register()``; the
    runtime drift WARN is the backstop for a deployment's own modules.
    """
    names: list[str] = []
    n = len(src)
    # A leading `#!` hashbang is a comment for the whole first line (valid in ES modules).
    i = 0
    if src.startswith("#!"):
        nl = src.find("\n")
        i = n if nl == -1 else nl + 1
    while i < n:
        c = src[i]

        if c == "/" and i + 1 < n and src[i + 1] == "/":
            nl = src.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end_c = src.find("*/", i + 2)
            i = n if end_c == -1 else end_c + 2
            continue
        if c in "'\"`":
            i = _skip_js_string(src, i)
            continue
        if c == "/":
            # Ambiguous: division or a regex. Abandon the line rather than risk a false error.
            nl = src.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue

        after_paren = _match_server_tool_call(src, i)
        if after_paren is None:
            i += 1
            continue

        arg = _skip_ws_and_comments(src, after_paren)
        if arg < n and src[arg] in "'\"":
            end_s = _skip_js_string(src, arg)
            candidate = src[arg + 1:end_s - 1] if end_s > arg + 1 else ""
            # Only a COMPLETE literal argument counts: anything other than ',' or ')' next means
            # the name is an expression (concatenation, method call, ...).
            following = _skip_ws_and_comments(src, end_s)
            if following < n and src[following] in ",)" and _TOOL_NAME_FORM.fullmatch(candidate):
                names.append(candidate)
            i = end_s
        else:
            i = after_paren

    return names


def _literal_server_tool_names(module_dir: Path) -> dict[str, str]:
    """Map literal ``server.tool('<name>')`` name -> the toolbox file registering it."""
    found: dict[str, str] = {}
    toolbox_dir = module_dir / "toolbox"
    if not toolbox_dir.is_dir():
        return found
    for js in sorted(toolbox_dir.glob("*.js")):
        for name in _scan_server_tool_literals(js.read_text()):
            found.setdefault(name, js.name)
    return found


def validate_tool_namespace(
    declarations: Iterable[tuple[str, list]],
) -> dict[str, list[str]]:
    """Cross-manifest tool-namespace checks. Returns ``{module_name: [errors]}``.

    Enablement is keyed by tool NAME alone (``tools/<name>/is_enabled``) and every
    module's ``config.json`` defaults are merged by literal path, so two modules
    sharing a tool name would share one switch and let merge order decide whose
    default wins.

    Takes ``(module_name, tools_list)`` pairs rather than reading the filesystem, so
    ``validate_all`` (parsed JSON) and ``setup:upgrade`` (``ModuleManifest.tools``)
    run the SAME check. That matters: ``setup._validate_manifests`` validates one
    module at a time and would otherwise never see a collision.

    Same-module duplicates are reported by ``_validate_module`` (so a scoped
    ``module:validate <name>`` catches them); this pass reports CROSS-module
    collisions only, so a duplicate is never reported twice.
    """
    results: dict[str, list[str]] = {}
    name_owner: dict[str, str] = {}

    for module_name, tools in declarations:
        if not isinstance(tools, list):
            continue
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_name = tool.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                continue
            other = name_owner.get(tool_name)
            if other is not None and other != module_name:
                results.setdefault(module_name, []).append(
                    f"module.json: tool name '{tool_name}' is also declared by module "
                    f"'{other}' — tool names must be globally unique (enablement is keyed "
                    "by name alone)"
                )
            else:
                name_owner[tool_name] = module_name

    return results


def _declared_tool_names(tools: list) -> set[str]:
    """Names declared in a manifest's ``tools[]`` (ignoring malformed entries)."""
    return {
        str(t["name"]) for t in tools
        if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"]
    }


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

            if "requires" in tool:
                req = tool["requires"]
                if not isinstance(req, str) or not req:
                    errors.append(
                        f"module.json: tools[{i}] 'requires' must be a non-empty string naming a "
                        "tool declared in this module's tools[]"
                    )
                elif req == tool.get("name"):
                    errors.append(f"module.json: tools[{i}] 'requires' must not be self-referential")
                elif req not in _declared_tool_names(tools):
                    errors.append(
                        f"module.json: tools[{i}] 'requires' must name a tool declared in this "
                        f"module's tools[] (got '{req}')"
                    )

        # Same-module duplicates are locally detectable, so a scoped `module:validate <name>`
        # must catch them; validate_tool_namespace reports CROSS-module collisions only.
        seen_names: set[str] = set()
        for i, tool in enumerate(tools):
            if not isinstance(tool, dict):
                continue
            tool_name = tool.get("name")
            if isinstance(tool_name, str) and tool_name:
                if tool_name in seen_names:
                    errors.append(
                        f"module.json: tools[{i}] name '{tool_name}' is declared twice in this "
                        "module — enablement is keyed by name, so the duplicate is the same switch"
                    )
                seen_names.add(tool_name)

        # A cycle would make the toolbox gate deny every tool on it (enabledCheck fails closed),
        # while a naive display walk would report them unblocked. Reject it at the source instead
        # of asking both languages to agree on how to render an impossible manifest.
        edges = {
            str(t["name"]): t["requires"]
            for t in tools
            if isinstance(t, dict) and isinstance(t.get("name"), str)
            and isinstance(t.get("requires"), str) and t["requires"]
        }
        for start in sorted(edges):
            path = [start]
            seen = {start}
            current = edges.get(start)
            while current is not None:
                path.append(current)
                if current in seen:
                    errors.append(
                        "module.json: 'requires' cycle among tools: " + " -> ".join(path)
                    )
                    break
                seen.add(current)
                current = edges.get(current)

    declared_names = _declared_tool_names(tools) if isinstance(tools, list) else set()
    module_name = str(manifest.get("name") or module_dir.name)
    for tool_name, js_file in _literal_server_tool_names(module_dir).items():
        if tool_name in declared_names:
            continue
        errors.append(
            f"toolbox: tool '{tool_name}' is registered by {js_file} but not declared in "
            f"module.json tools[] — it is invisible in the admin Tools screen and in "
            f'tool:list; add {{"type": "mcp", "name": "{tool_name}", "description": "...", '
            f'"toolset": "{module_name}"}}'
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
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError as e:
            errors.append(f"config.json: invalid JSON — {e}")
            config = None

        if isinstance(config, dict):
            # `tools/<name>/is_enabled` is merged across every module by literal path, so a
            # default here silently applies to whatever module owns <name>. Require ownership.
            for key in config:
                m = _TOOL_ENABLED_KEY.fullmatch(str(key))
                if m and m.group(1) not in declared_names:
                    errors.append(
                        f"config.json: '{key}' sets a default for a tool this module does not "
                        "declare in module.json tools[]"
                    )

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

    for module_name, errs in validate_tool_namespace(
        (name, manifest.get("tools", [])) for name, manifest in all_modules.items()
    ).items():
        results.setdefault(module_name, []).extend(errs)

    # Cross-validate sequence references
    available_names = set(all_modules.keys())
    for name, manifest in all_modules.items():
        for dep in manifest.get("sequence", []):
            if dep not in available_names:
                results.setdefault(name, []).append(
                    f"module.json: sequence dependency '{dep}' not found on disk"
                )

    return results
