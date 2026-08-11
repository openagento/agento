"""Off-disk module discovery — shared by manifest enumeration and CLI provisioning.

Moved out of ``cli/_provisioning.py`` (where it was a private helper) so core
contracts such as ``framework/harness/`` can use it without depending on the CLI
layer. Behaviour is unchanged.
"""
from __future__ import annotations

import importlib.resources as ires
from pathlib import Path

from .module_status import read_module_status


def iter_module_dirs(project_root: Path | None) -> list[Path]:
    """Yield module directories the framework would enumerate at install/upgrade time.

    Order matters for shadowing rules — same as ``resolve_module_source``:
    local (``app/code/``) wins, then core (framework's bundled modules), then
    PyPI extensions in the project venv. When ``project_root`` is None (fresh
    install — project doesn't exist yet) only core modules contribute.
    """
    dirs: list[Path] = []
    seen: set[str] = set()

    if project_root is not None:
        app_code = project_root / "app" / "code"
        if app_code.is_dir():
            for entry in sorted(app_code.iterdir()):
                if entry.is_dir() and (entry / "module.json").is_file():
                    dirs.append(entry)
                    seen.add(entry.name)

    # Core modules ship inside the installed wheel under agento.modules.
    try:
        core_root = ires.files("agento.modules")
    except (FileNotFoundError, ModuleNotFoundError):
        core_root = None
    if core_root is not None:
        for entry in sorted(core_root.iterdir(), key=lambda e: e.name):
            if entry.name.startswith("_") or not entry.is_dir():
                continue
            if entry.name in seen:
                continue
            # ires.files returns Traversable; we need a real Path for parsers.
            with ires.as_file(entry) as p:
                if (p / "module.json").is_file():
                    dirs.append(Path(p))
                    seen.add(entry.name)

    # PyPI extensions inside the containers: compose bind-mounts each one at
    # `/opt/agento-src/<ext>` (see cli/_provisioning.mount_block), NOT into a .venv. Without
    # this branch a PyPI harness is invisible to `config:set` / `config:schema` / admin when
    # they run in cron — which is where they actually run.
    for entry in _container_extension_dirs():
        if entry.name not in seen:
            dirs.append(entry)
            seen.add(entry.name)

    if project_root is not None:
        venv = project_root / ".venv"
        for site_packages in venv.glob("lib/python*/site-packages"):
            # PyPI extensions live at <site-packages>/<name>/module.json (the
            # package itself acts as a module).
            for entry in sorted(site_packages.iterdir()):
                if entry.name.startswith("_") or not entry.is_dir():
                    continue
                if entry.name in seen:
                    continue
                if (entry / "module.json").is_file():
                    dirs.append(entry)
                    seen.add(entry.name)

    return dirs


def iter_enabled_module_dirs(project_root: Path | None) -> list[Path]:
    """Same as :func:`iter_module_dirs`, minus modules disabled in ``app/etc/modules.json``."""
    status: dict[str, bool] = {}
    if project_root is not None:
        status_path = project_root / "app" / "etc" / "modules.json"
        if status_path.is_file():
            status = read_module_status(status_path)

    # Default-enabled when modules.json is absent or doesn't list this module.
    return [
        d for d in iter_module_dirs(project_root)
        if not status or status.get(d.name, True)
    ]


def resolve_module_root() -> Path | None:
    """The project root this process should enumerate modules from.

    Two very different execution contexts need the SAME module set:

    * **host CLI** (``agento config:set`` in a project) — walk up to the project root;
    * **inside the cron container** — there is no project dir, but ``app/code`` is bind
      mounted at ``/app/code`` (``bootstrap.USER_MODULES_DIR``), so ``/`` is the root that
      makes ``<root>/app/code`` resolve.

    Returning ``None`` (neither found) keeps the core-modules-only behaviour, which is
    correct during a fresh install when no project exists yet. Without this, dynamic
    ``options_source`` fields silently offered core harnesses only — so a harness shipped
    by an ``app/code`` or PyPI module could never be selected, defeating the whole
    "third harness with no framework edits" promise.
    """
    from .project import find_project_root

    try:
        root = find_project_root()
    except Exception:
        root = None
    if root is not None:
        return root

    from .bootstrap import USER_MODULES_DIR

    user_dir = Path(USER_MODULES_DIR)
    if user_dir.is_dir() and user_dir.name == "code":
        return user_dir.parent.parent  # /app/code -> /
    return None


# Where compose bind-mounts PyPI extensions inside the cron/consumer containers.
CONTAINER_EXTENSION_DIR = "/opt/agento-src"


def _container_extension_dirs(root: str | None = None) -> list[Path]:
    """Module dirs bind-mounted as PyPI extensions in the container.

    ``/opt/agento-src/agento`` is the framework package itself and carries no
    ``module.json``, so the manifest check excludes it without needing a name special-case.
    """
    base = Path(root or CONTAINER_EXTENSION_DIR)
    if not base.is_dir():
        return []
    return [
        entry for entry in sorted(base.iterdir())
        if entry.is_dir()
        and not entry.name.startswith(("_", "."))
        and (entry / "module.json").is_file()
    ]


def scan_all_modules(core_dir: str, user_dir: str) -> list:
    """Every module manifest the framework should consider, from ONE discovery path.

    ``bootstrap``, ``setup:upgrade`` and ``module:validate`` must agree on the module set,
    or a module can register at runtime while its manifest validation, SQL, data patches,
    cron and onboarding are silently skipped — which is exactly what happened to PyPI
    extensions when only ``bootstrap`` learned about the container mount.

    Order encodes shadowing: core, then local ``app/code`` (which may override core), then
    container-mounted PyPI extensions, which never shadow a name already present.
    """
    from .module_loader import scan_modules

    manifests = list(scan_modules(core_dir)) + list(scan_modules(user_dir))
    seen = {m.name for m in manifests}
    for m in scan_modules(CONTAINER_EXTENSION_DIR):
        if m.name not in seen:
            manifests.append(m)
            seen.add(m.name)
    return manifests


def module_dirs_for_validation(core_dir, user_dir) -> list[tuple[str, Path]]:
    """``[(module_name, module_dir)]`` for validation, honouring shadowing.

    Same set and same precedence as :func:`scan_all_modules` (core, then local ``app/code``
    which may override core, then container-mounted extensions which never shadow an existing
    name) — but returned as directories, because validation reads the raw manifest files
    rather than parsed manifests.
    """
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for scan_dir in (Path(core_dir), Path(user_dir), Path(CONTAINER_EXTENSION_DIR)):
        if not scan_dir.is_dir():
            continue
        for entry in sorted(scan_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith(("_", ".")):
                continue
            if not (entry / "module.json").is_file():
                continue
            if entry.name in seen:
                # A later root must not shadow an earlier one; app/code overriding core is
                # expressed by scanning user_dir second and replacing in place.
                if scan_dir == Path(user_dir):
                    out = [(n, p) for n, p in out if n != entry.name]
                else:
                    continue
            out.append((entry.name, entry))
            seen.add(entry.name)
    return out
