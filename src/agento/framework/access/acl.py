"""The ACL registry: panel leaves and scoped operations, read from ``acl.json`` files.

The framework file next to this one holds the core screens (``admin.*``) and the framework
operation ``artifact.launch``. Each module may ship its own ``acl.json`` (the Magento
``etc/acl.xml``) with the same shape::

    {"screens": [{"title": "...", "leaves": [{"id": "<module>.<name>", "title": "..."}]}],
     "operations": [{"id": "<module>.<name>", "title": "..."}]}

A leaf is a global grant that a panel route names in its ``acl``. An operation is a grant in
one workspace or agent_view that a handler checks. This file names no module: it only reads
files (PLC-1).
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

CORE_FILE = Path(__file__).with_name("acl.json")
FILE_NAME = "acl.json"
# The two route `acl` values that are not a leaf (Magento webapi `self` and `anonymous`).
SELF, ANONYMOUS = "self", "anonymous"
_KEYS = {"screens", "operations"}
_CORE_ID = re.compile(r"^[a-z_]+(\.[a-z_]+)*$")


class AclError(ValueError):
    """An ``acl.json`` that is not valid, or an id that two files declare."""


@dataclass(frozen=True)
class Acl:
    # ((screen title, ((leaf id, leaf title), ...)), ...): core first, then modules by name.
    screens: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    # leaf id -> owner module ("" for core).
    leaf_owners: dict[str, str]
    # operation id -> title.
    operations: dict[str, str]
    # Operations of the framework file: a grant for every role, admin included.
    core_operations: frozenset[str]

    @property
    def resources(self) -> frozenset[str]:
        return frozenset(self.leaf_owners)


def _id_re(module: str | None) -> re.Pattern:
    return _CORE_ID if module is None else re.compile(rf"^{re.escape(module)}(\.[a-z_]+)*$")


def _entries(value) -> list | None:
    return value if isinstance(value, list) else None


def _entry_error(entry, id_re: re.Pattern, where: str) -> str | None:
    if not (isinstance(entry, dict) and set(entry) == {"id", "title"} and isinstance(entry["id"], str)
            and isinstance(entry["title"], str) and entry["title"].strip()):
        return f"{where}: {entry!r} must be {{id, title}} with a non-empty title"
    if entry["id"] in (SELF, ANONYMOUS) or not id_re.match(entry["id"]):
        return f"{where}: id {entry['id']!r} must match {id_re.pattern}"
    return None


def errors(module: str | None, data: object) -> list[str]:
    """Why this ``acl.json`` content is refused (``module`` None: the framework file).

    One function for ``module:validate`` and for ``load``, so the two cannot disagree.
    """
    if not isinstance(data, dict):
        return ["acl.json must be an object with 'screens' and/or 'operations'"]
    out = [f"acl.json: unknown key {k!r}" for k in sorted(set(data) - _KEYS)]
    id_re, ids = _id_re(module), []
    screens = _entries(data.get("screens", []))
    if screens is None:
        out.append("acl.json: 'screens' must be a list")
    for screen in screens or []:
        leaves = screen.get("leaves") if isinstance(screen, dict) else None
        if not (isinstance(screen, dict) and set(screen) == {"title", "leaves"} and isinstance(screen["title"], str)
                and screen["title"].strip() and isinstance(leaves, list) and leaves):
            out.append(f"acl.json: screen {screen!r} must be {{title, leaves}} with at least one leaf")
            continue
        for leaf in leaves:
            problem = _entry_error(leaf, id_re, "acl.json: leaf")
            out += [problem] if problem else []
            ids += [] if problem else [leaf["id"]]
    operations = _entries(data.get("operations", []))
    if operations is None:
        out.append("acl.json: 'operations' must be a list")
    for op in operations or []:
        problem = _entry_error(op, id_re, "acl.json: operation")
        out += [problem] if problem else []
        ids += [] if problem else [op["id"]]
    out += [f"acl.json: id {i!r} is declared twice" for i in sorted({i for i in ids if ids.count(i) > 1})]
    return out


def read(module_dir: Path) -> object | None:
    """A module's ``acl.json`` content, ``{}`` when it has none, None when it cannot be parsed."""
    path = Path(module_dir) / FILE_NAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _ids(data: dict) -> list[str]:
    return [leaf["id"] for s in data.get("screens", []) for leaf in s["leaves"]] + \
        [op["id"] for op in data.get("operations", [])]


def _core_data() -> dict:
    data = json.loads(CORE_FILE.read_text())
    if problems := errors(None, data):
        raise AclError(f"framework acl.json: {'; '.join(problems)}")
    return data


def duplicates(module_dirs: Sequence[tuple[str, Path]]) -> list[tuple[str, str]]:
    """``[(module, message)]`` for each id that the framework file or an earlier module
    already declares. Ids are unique across core and every module. A file that is not valid
    is skipped here: ``errors`` reports it."""
    owners = dict.fromkeys(_ids(_core_data()), "the framework")
    out = []
    for name, module_dir in module_dirs:
        data = read(module_dir)
        if data is None or errors(name, data):
            continue
        for i in _ids(data):
            if i in owners:
                out.append((name, f"acl.json: id {i!r} is already declared by {owners[i]}"))
            else:
                owners[i] = f"module {name!r}"
    return out


def load(module_dirs: Sequence[tuple[str, Path]]) -> Acl:
    """The registry of the framework file plus each given module's ``acl.json``.

    Raises ``AclError`` for a file that is not valid or an id declared twice: a leaf that
    nobody can resolve must stop the caller, not grant less in silence.
    """
    core = _core_data()
    problems = []
    found: list[tuple[str, dict]] = []
    for name, module_dir in sorted(module_dirs):
        data = read(module_dir)
        if data is None:
            problems.append(f"module {name}: acl.json is not valid JSON")
        elif bad := errors(name, data):
            problems += [f"module {name}: {p}" for p in bad]
        elif data:
            found.append((name, data))
    problems += [f"module {name}: {msg}" for name, msg in duplicates(sorted(module_dirs))]
    if problems:
        raise AclError("; ".join(problems))
    screens, owners, operations = [], {}, {}
    for owner, data in [("", core), *found]:
        for s in data.get("screens", []):
            screens.append((s["title"], tuple((leaf["id"], leaf["title"]) for leaf in s["leaves"])))
            owners.update((leaf["id"], owner) for leaf in s["leaves"])
        operations.update((op["id"], op["title"]) for op in data.get("operations", []))
    return Acl(screens=tuple(screens), leaf_owners=owners, operations=operations,
               core_operations=frozenset(op["id"] for op in core.get("operations", [])))


@cache
def core() -> Acl:
    """The framework file alone: the core leaves (``/api/admin/scopes``)."""
    return load([])


def route_ids(acl: Acl, module: str, sequence: Sequence[str]) -> frozenset[str]:
    """The leaves a route of ``module`` may name: its own, core, and those of each module in
    its ``sequence`` (MOD-1: a module names only what it depends on)."""
    allowed = {"", module, *sequence}
    return frozenset(i for i, owner in acl.leaf_owners.items() if owner in allowed)


_REGISTRY: Acl | None = None


def install(acl: Acl) -> None:
    """Set the process registry: ``web`` does it once, from the module set its routes came from."""
    global _REGISTRY
    _REGISTRY = acl


def registry() -> Acl:
    """The process registry; on first use (a CLI command), the enabled modules' files.

    Cached for the process, like ``web``'s route table: a module enable or disable takes
    effect at the next start.
    """
    global _REGISTRY
    if _REGISTRY is None:
        from ..bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
        from ..module_discovery import module_dirs_by_name
        from ..module_status import is_enabled, read_module_status

        status = read_module_status()
        _REGISTRY = load([(n, d) for n, d in module_dirs_by_name(CORE_MODULES_DIR, USER_MODULES_DIR)
                          if is_enabled(n, status)])
    return _REGISTRY
