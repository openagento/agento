"""Read tester declarations off disk — `system.json` only, no `bootstrap()`.

A declaration lives on the field it tests:

    "smtp_password": {"type": "obscure", "tester": {"kind": "smtp", …}}
    "outlook_client_secret": {"type": "obscure", "tester": "graph_credentials"}
    "identity/ssh_private_key": {"type": "obscure",
                                 "tester": {"kind": "local", "class": "src.testers.x.T"}}

Everything here fails closed and never raises: `config:get`, the admin TUI and
`config:test` all call it, and a hand-edited manifest must degrade to "no test
button", not to a traceback. `module:validate` (Task 3) is where a broken
declaration is *reported*.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .protocols import KIND_LOCAL, KIND_TOOLBOX, TesterRef


@lru_cache(maxsize=256)
def field_schemas(module_dir: Path) -> dict | None:
    """A module's parsed ``system.json``, or ``None`` when it is absent,
    unparsable, or not a JSON object. ``None`` means "nothing can be said" —
    never "no fields"."""
    path = module_dir / "system.json"
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _module_dir_for(module_name: str, project_root: Path | None) -> Path | None:
    from ..module_discovery import iter_enabled_module_dirs

    wanted = {module_name, module_name.replace("_", "-")}
    return next(
        (d for d in iter_enabled_module_dirs(project_root) if d.name in wanted),
        None,
    )


def field_schema_for_path(path: str, project_root: Path | None = None) -> dict | None:
    """The field's ``system.json`` entry, or ``None`` if there isn't one."""
    from ..core_config import _parse_config_path

    parsed = _parse_config_path(path)
    if parsed is None:
        return None
    module_name, tool_name, field_name = parsed
    if tool_name is not None:
        return None  # tool fields are gated by is_enabled, not tested
    module_dir = _module_dir_for(module_name, project_root)
    if module_dir is None:
        return None
    schemas = field_schemas(module_dir)
    if schemas is None:
        return None
    entry = schemas.get(field_name)
    return entry if isinstance(entry, dict) else None


def _ref_from(raw, module: str, module_dir: Path) -> TesterRef | None:
    """Normalize a ``tester`` value. A bare string names a toolbox probe."""
    if isinstance(raw, str):
        name = raw.strip()
        return TesterRef(KIND_TOOLBOX, name, module, module_dir) if name else None
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if not isinstance(kind, str) or not kind:
        return None
    if kind == KIND_LOCAL:
        class_path = raw.get("class")
        if not isinstance(class_path, str) or not class_path:
            return None
        return TesterRef(KIND_LOCAL, KIND_LOCAL, module, module_dir, class_path)
    if kind == KIND_TOOLBOX:
        # `{"kind": "toolbox", "name": "x"}` is what the bare string `"x"` is
        # sugar for, and the toolbox runs both. Label it by the probe NAME:
        # labelling it "toolbox" told the operator which arm runs the test, not
        # which test runs, and the sugar form already shows the name.
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        return TesterRef(KIND_TOOLBOX, name.strip(), module, module_dir)
    # Every other kind is a built-in probe the toolbox interprets, not here.
    return TesterRef(KIND_TOOLBOX, kind, module, module_dir)


def tester_label(module: str, module_dir, field_schema) -> str:
    """A short display label for the tester declared on a field, or "" for none.

    Thin on purpose: `_ref_from` is the single normalizer for all three
    declaration forms, so the admin TUI cannot disagree with the runner about
    which fields are testable.
    """
    if not isinstance(field_schema, dict):
        return ""
    ref = _ref_from(field_schema.get("tester"), module, module_dir)
    return ref.label if ref is not None else ""


def tester_for_field(path: str, project_root: Path | None = None) -> TesterRef | None:
    """What this field's ``tester`` key declares, or ``None``."""
    from ..core_config import _parse_config_path

    schema = field_schema_for_path(path, project_root)
    if schema is None:
        return None
    parsed = _parse_config_path(path)
    if parsed is None:
        return None
    module_name = parsed[0]
    module_dir = _module_dir_for(module_name, project_root)
    if module_dir is None:
        return None
    return _ref_from(schema.get("tester"), module_name, module_dir)


def enumerate_testable_fields(project_root: Path | None = None) -> list[str]:
    """Every ``module/field`` path whose field declares a tester, module order."""
    from ..module_discovery import iter_enabled_module_dirs

    out: list[str] = []
    for module_dir in iter_enabled_module_dirs(project_root):
        schemas = field_schemas(module_dir)
        if not schemas:
            continue
        module_name = module_dir.name.replace("-", "_")
        for field_name, schema in schemas.items():
            if not isinstance(schema, dict):
                continue
            if _ref_from(schema.get("tester"), module_name, module_dir) is not None:
                out.append(f"{module_name}/{field_name}")
    return out


def enumerate_test_groups(
    project_root: Path | None = None,
) -> list[tuple[str, tuple[str, ...]]]:
    """``(representative_path, every_path_sharing_that_test)``, module order.

    One declaration is deliberately attached to several fields — every SMTP field
    carries the same `smtp` block, the six Graph fields share one named probe, and
    both SSH identity fields run the same pair check. Running the test per FIELD
    means six real logins for one Outlook credential, which is rate-limit and
    account-lockout pressure for no extra information. The grouping key is the
    NORMALIZED declaration plus the module, so `jira/jira_token` and
    `jira/jira_admin_token` — same kind, different credential — stay two tests,
    while `"x"` and `{"kind": "toolbox", "name": "x"}` group as the one test they
    are. Keying on the raw JSON made the two spellings two logins, the second of
    which then answered COOLDOWN.
    """
    import json

    from ..module_discovery import iter_enabled_module_dirs

    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for module_dir in iter_enabled_module_dirs(project_root):
        schemas = field_schemas(module_dir)
        if not schemas:
            continue
        module_name = module_dir.name.replace("-", "_")
        for field_name, schema in schemas.items():
            if not isinstance(schema, dict):
                continue
            tester = schema.get("tester")
            ref = _ref_from(tester, module_name, module_dir)
            if ref is None:
                continue
            # The normalized reference, not the spelling. A named probe's
            # identity is its NAME (both spellings reach the same probe over the
            # same credential); a built-in kind's identity includes the spec,
            # because that is what names the credential.
            named = isinstance(tester, str) or (
                isinstance(tester, dict) and tester.get("kind") == KIND_TOOLBOX
            )
            if ref.kind == KIND_LOCAL:
                identity = (KIND_LOCAL, ref.class_path)
            elif named:
                identity = (KIND_TOOLBOX, ref.label)
            else:
                identity = (KIND_TOOLBOX, ref.label, tester)
            try:
                shape = json.dumps(identity, sort_keys=True, default=repr)
            except (TypeError, ValueError):
                shape = repr(identity)
            key = f"{module_name}\x00{shape}"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(f"{module_name}/{field_name}")
    return [(groups[k][0], tuple(groups[k])) for k in order]


__all__ = [
    "KIND_LOCAL", "KIND_TOOLBOX", "enumerate_test_groups",
    "enumerate_testable_fields", "field_schema_for_path", "field_schemas",
    "tester_for_field", "tester_label",
]
