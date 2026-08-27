"""Module loader — scans modules/*/module.json and imports declared classes."""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ModuleManifest:
    """Parsed module.json manifest."""

    name: str
    version: str
    description: str
    path: Path
    provides: dict[str, list[dict]] = field(default_factory=dict)
    tools: list[dict] = field(default_factory=list)
    log_servers: list[dict] = field(default_factory=list)
    config: dict[str, dict] = field(default_factory=dict)
    observers: dict[str, list[dict]] = field(default_factory=dict)  # events.json
    data_patches: dict = field(default_factory=dict)  # data_patch.json
    cron: dict = field(default_factory=dict)  # cron.json (cron job declarations)
    sequence: list[str] = field(default_factory=list)  # Magento-style: modules this depends on
    order: int = 1000  # Sort position within dependency tier


def _read_json(path: Path) -> dict | list | None:
    """Read a JSON file if it exists. Returns None if absent or malformed."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def scan_modules(modules_dir: str = "/modules") -> list[ModuleManifest]:
    """Scan modules directory and return parsed manifests.

    Skips directories starting with ``_`` (e.g. ``_example``).
    """
    base = Path(modules_dir)
    if not base.is_dir():
        return []

    manifests: list[ModuleManifest] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        manifest_path = entry / "module.json"
        if not manifest_path.exists():
            continue
        data = json.loads(manifest_path.read_text())

        # Magento-style: read companion JSON files for each concern.
        # Falls back to module.json inline sections for backward compatibility.
        provides = _read_json(entry / "di.json") or data.get("provides", {})
        observers = _read_json(entry / "events.json") or data.get("observers", {})
        config = _read_json(entry / "system.json") or data.get("config", {})
        data_patches = _read_json(entry / "data_patch.json") or data.get("data_patches", {})
        cron = _read_json(entry / "cron.json") or data.get("cron", {})

        manifests.append(
            ModuleManifest(
                name=data.get("name", entry.name),
                version=data.get("version", "0.0.0"),
                description=data.get("description", ""),
                path=entry,
                provides=provides,
                observers=observers,
                tools=data.get("tools", []),
                log_servers=data.get("log_servers", []),
                config=config,
                data_patches=data_patches,
                cron=cron,
                sequence=data.get("sequence", []),
                order=data.get("order", 1000),
            )
        )
    return manifests


def _try_package_import(module_dir: Path, module_dotted: str, class_name: str) -> type | None:
    """Try to import via the normal Python package system.

    Works for core modules that are part of the ``agento`` package
    (e.g. ``src/agento/modules/jira/src/channel.py`` → ``agento.modules.jira.src.channel``).
    Returns None for user modules not on the Python path.
    """
    try:
        # Core modules live under agento.modules.<name>
        # Detect by checking if "modules" is a parent of module_dir
        parts = module_dir.parts
        try:
            # Find the last "modules" directory (avoids matching repo-level dirs)
            modules_idx = len(parts) - 1 - list(reversed(parts)).index("modules")
        except ValueError:
            return None
        # module_dir is e.g. .../src/agento/modules/jira → module name is parts[modules_idx+1]
        # Package path: agento.modules.<name>.<module_dotted>
        module_name = parts[modules_idx + 1]
        full_module = f"agento.modules.{module_name}.{module_dotted}"
        mod = importlib.import_module(full_module)
        return getattr(mod, class_name)
    except (ImportError, AttributeError, IndexError):
        return None


def is_confined_class_path(module_dir: Path, class_path: str) -> bool:
    """True when every segment is an identifier AND the file stays under the module.

    Both halves are needed. The identifier check rejects `src/t.T` and `src.t-2.T`
    before they reach the filesystem; the `relative_to` check is what stops
    `..src.t.T` and a symlink pointing out of the module — hence `resolve()` on
    both sides.
    """
    parts = class_path.split(".")
    if len(parts) < 2 or not all(part.isidentifier() for part in parts):
        return False
    try:
        target = (module_dir / (".".join(parts[:-1]).replace(".", "/") + ".py")).resolve()
        target.relative_to(module_dir.resolve())
    except (ValueError, OSError):
        return False
    return True


def import_class(module_dir: Path, class_path: str) -> type:
    """Import a class from a module directory.

    ``class_path`` is a dotted path like ``src.channel.JiraChannel``.
    The last segment is the class name; the rest map to a file path
    relative to *module_dir*.

    For core modules (part of the ``agento`` package), uses normal Python
    imports so the class identity is shared with test code. Falls back to
    ``spec_from_file_location`` for user modules in ``app/code/``.

    Example::

        import_class(Path("/modules/jira"), "src.channel.JiraChannel")
        # loads /modules/jira/src/channel.py and returns the JiraChannel class
    """
    if not is_confined_class_path(module_dir, class_path):
        raise ValueError(
            f"class_path must be a dotted path inside the module, got: {class_path!r}"
        )
    parts = class_path.rsplit(".", 1)
    module_dotted, class_name = parts

    # Convert dotted module path to file path
    rel_path = module_dotted.replace(".", os.sep) + ".py"
    file_path = module_dir / rel_path
    if not file_path.exists():
        raise FileNotFoundError(
            f"Module file not found: {file_path} (from class_path={class_path!r})"
        )

    # Try normal Python import first (core modules on the package path)
    cls = _try_package_import(module_dir, module_dotted, class_name)
    if cls is not None:
        return cls

    # Fallback: isolated import for user modules (app/code/)
    spec_name = f"agento_module.{module_dir.name}.{module_dotted}"
    spec = importlib.util.spec_from_file_location(spec_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create import spec for {file_path}")

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if not hasattr(mod, class_name):
        raise AttributeError(
            f"{file_path} does not define {class_name!r}"
        )
    return getattr(mod, class_name)
