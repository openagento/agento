from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _module_dirs() -> tuple[Path, Path]:
    """Resolve core and user module directories (Docker vs local dev).

    Core modules ship with the installed package (works for both uv-tool
    installs and source-layout checkouts). User modules live at ``/app/code``
    inside Docker, otherwise under ``<cwd>/app/code`` for host-side CLI runs.
    """
    from ..bootstrap import CORE_MODULES_DIR

    core_dir = Path(CORE_MODULES_DIR)
    user_dir = Path("/app/code") if Path("/app/code").is_dir() else Path.cwd() / "app" / "code"
    return core_dir, user_dir


def _set_module_state(name: str, enabled: bool) -> None:
    """Enable or disable a module by name.

    On the host, resolves the source via ``app/code/`` (local) and the project
    venv (PyPI extension) — toggling a PyPI extension also regenerates
    ``docker-compose.yml`` and restarts containers (mounts changed). Inside
    Docker (no project root), falls back to scanning the core/user module
    directories.
    """
    from ..module_status import set_enabled
    from ._project import compose_file_flags, find_project_root

    project_root = find_project_root()

    source: str
    if project_root is not None:
        from ..module_status import resolve_module_source
        source = resolve_module_source(name, project_root)
    else:
        # In-container fallback: only modules visible to scan can be toggled.
        from ..module_loader import scan_modules

        core_dir, user_dir = _module_dirs()
        all_names = {m.name for m in scan_modules(str(core_dir)) + scan_modules(str(user_dir))}
        if name not in all_names:
            print(f"Module '{name}' not found")
            sys.exit(1)
        source = "local"

    if source == "missing":
        print(f"Module '{name}' not found.")
        print(f"  - For local modules: place under app/code/<vendor>/{name}/ with module.json")
        print(f"  - For PyPI extensions: run 'uv add {name}' first, then re-run module:enable")
        sys.exit(1)

    set_enabled(name, enabled)
    state = "enabled" if enabled else "disabled"
    print(f"Module '{name}' {state}")

    # PyPI extension toggle changes container mounts — regenerate compose
    # and bounce the running stack. Local modules already mount via app/code/.
    if source == "pypi" and project_root is not None:
        from ._provisioning import regenerate_compose

        regenerate_compose(project_root)
        print("Regenerated docker-compose.yml")

        flags = compose_file_flags(project_root)
        if flags:
            result = subprocess.run(
                ["docker", "compose", *flags, "up", "-d"],
            )
            if result.returncode != 0:
                print("Warning: 'docker compose up -d' failed — restart manually for the mount change to take effect.")


class MakeModuleCommand:
    @property
    def name(self) -> str:
        return "make:module"

    @property
    def shortcut(self) -> str:
        return "ma:mo"

    @property
    def help(self) -> str:
        return "Scaffold a new user module"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("name", help="Module name (lowercase, alphanumeric + hyphens)")
        parser.add_argument("--description", default="", help="Module description")
        parser.add_argument("--tool", action="append", default=[], help="Tool spec: type:name:description")
        parser.add_argument("--base-dir", default=None, dest="base_dir", help="Base directory for module")

    def execute(self, args: argparse.Namespace) -> None:
        from ..module_scaffold import scaffold_module

        if args.base_dir:
            base_dir = Path(args.base_dir)
        else:
            docker_path = Path("/app/code")
            base_dir = docker_path if docker_path.is_dir() else Path(__file__).resolve().parents[4] / "app" / "code"

        module_dir = scaffold_module(
            name=args.name,
            base_dir=base_dir,
            description=args.description,
            tools=args.tool,
        )
        print(f"Module '{args.name}' created at {module_dir}")


class ModuleEnableCommand:
    @property
    def name(self) -> str:
        return "module:enable"

    @property
    def shortcut(self) -> str:
        return "mo:en"

    @property
    def help(self) -> str:
        return "Enable a module"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("name", help="Module name")

    def execute(self, args: argparse.Namespace) -> None:
        _set_module_state(args.name, True)


class ModuleDisableCommand:
    @property
    def name(self) -> str:
        return "module:disable"

    @property
    def shortcut(self) -> str:
        return "mo:di"

    @property
    def help(self) -> str:
        return "Disable a module"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("name", help="Module name")

    def execute(self, args: argparse.Namespace) -> None:
        _set_module_state(args.name, False)


class ModuleListCommand:
    @property
    def name(self) -> str:
        return "module:list"

    @property
    def shortcut(self) -> str:
        return "mo:li"

    @property
    def help(self) -> str:
        return "List all modules and their status"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def execute(self, args: argparse.Namespace) -> None:
        from ..dependency_resolver import resolve_order
        from ..module_loader import scan_modules
        from ..module_status import is_enabled, read_module_status

        core_dir, user_dir = _module_dirs()
        all_modules = resolve_order(scan_modules(str(core_dir)) + scan_modules(str(user_dir)))
        status = read_module_status()

        for m in all_modules:
            enabled = is_enabled(m.name, status)
            mark = "\u2714" if enabled else "\u2718"
            state = "enabled" if enabled else "disabled"
            seq = f" (requires: {', '.join(m.sequence)})" if m.sequence else ""
            print(f"  {mark} {m.name:20s} {state:10s} {m.version:8s} {m.description}{seq}")


class ModuleValidateCommand:
    @property
    def name(self) -> str:
        return "module:validate"

    @property
    def shortcut(self) -> str:
        return "mo:va"

    @property
    def help(self) -> str:
        return "Validate module structure and manifests"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("name", nargs="?", default=None, help="Module name (validates all if omitted)")

    def execute(self, args: argparse.Namespace) -> None:
        from ..module_validator import validate_all, validate_module

        core_dir, user_dir = _module_dirs()

        if args.name:
            # Validate specific module
            module_dir = None
            for search_dir in (core_dir, user_dir):
                candidate = search_dir / args.name
                if candidate.is_dir():
                    module_dir = candidate
                    break
            if module_dir is None:
                print(f"Module '{args.name}' not found")
                sys.exit(1)

            errors = validate_module(module_dir)
            if errors:
                print(f"\u2718 {args.name}")
                for err in errors:
                    print(f"  - {err}")
                sys.exit(1)
            else:
                print(f"\u2714 {args.name}")
                print("  note: tool-name collisions with OTHER modules are checked only by "
                      "'module:validate' without a module name, and by setup:upgrade")
        else:
            # Validate all
            results = validate_all(core_dir, user_dir)
            all_modules = set()
            for scan_dir in (core_dir, user_dir):
                if scan_dir.is_dir():
                    for entry in sorted(scan_dir.iterdir()):
                        if entry.is_dir() and not entry.name.startswith("_") and not entry.name.startswith("."):
                            all_modules.add(entry.name)

            has_errors = False
            for name in sorted(all_modules):
                if name in results:
                    print(f"\u2718 {name}")
                    for err in results[name]:
                        print(f"  - {err}")
                    has_errors = True
                else:
                    print(f"\u2714 {name}")

            if has_errors:
                sys.exit(1)
