"""Off-disk enumeration of ``agent_harnesses`` declarations — no Python import needed.

Three consumers need the harness list *without* ``bootstrap()``: ``config:set``
(validates ``select`` options by reading ``system.json`` from disk),
``enumerate_sandbox_packages`` at install/upgrade/doctor time (no DB, no imports), and
``module:validate`` inside ``setup:upgrade`` (must fail before any DB change).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..module_discovery import iter_enabled_module_dirs
from .descriptor import HarnessDescriptor, SandboxPackage

HARNESS_SECTION = "agent_harnesses"
LEGACY_SANDBOX_SECTION = "sandbox_packages"


@dataclass(frozen=True)
class HarnessDeclaration:
    """One ``agent_harnesses`` entry plus where it came from."""

    module: str
    module_dir: Path
    class_path: str
    descriptor: HarnessDescriptor
    raw: dict


def parse_harness_declarations(di_json: Path, module: str) -> list[HarnessDeclaration]:
    """Parse one module's ``di.json``. Returns [] when the file or section is absent.

    Raises ``ValueError`` on a malformed declaration — a typo must not silently
    drop a harness.
    """
    try:
        data = json.loads(di_json.read_text())
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"{di_json}: unreadable di.json — {e}") from None

    decls = data.get(HARNESS_SECTION, [])
    if not isinstance(decls, list):
        raise ValueError(f"{di_json}: {HARNESS_SECTION!r} must be an array")

    out: list[HarnessDeclaration] = []
    for decl in decls:
        if not isinstance(decl, dict):
            raise ValueError(f"{di_json}: each {HARNESS_SECTION} entry must be an object")
        class_path = decl.get("class")
        if not isinstance(class_path, str) or not class_path:
            raise ValueError(
                f"{di_json}: harness {decl.get('id')!r} is missing a 'class' path"
            )
        try:
            descriptor = HarnessDescriptor.from_declaration(decl)
        except ValueError as e:
            raise ValueError(f"{di_json}: {e}") from None
        out.append(
            HarnessDeclaration(
                module=module,
                module_dir=di_json.parent,
                class_path=class_path,
                descriptor=descriptor,
                raw=decl,
            )
        )
    return out


def enumerate_harness_declarations(
    project_root: Path | None = None,
) -> list[HarnessDeclaration]:
    """Every ``agent_harnesses`` declaration across enabled modules.

    Same shadowing rules and ``app/etc/modules.json`` filter as module loading.
    Raises ``RuntimeError`` on a duplicate harness id or credential scope across
    modules, so a collision surfaces in ``module:validate`` rather than at bootstrap.
    """
    declarations: list[HarnessDeclaration] = []
    by_id: dict[str, str] = {}
    by_scope: dict[str, str] = {}
    by_env_key: dict[str, str] = {}

    for module_dir in iter_enabled_module_dirs(project_root):
        for decl in parse_harness_declarations(module_dir / "di.json", module_dir.name):
            harness_id = decl.descriptor.id
            prior = by_id.get(harness_id)
            if prior is not None:
                raise RuntimeError(
                    f"duplicate harness id {harness_id!r} declared by both "
                    f"{prior!r} and {decl.module!r}"
                )
            by_id[harness_id] = decl.module

            for scope in decl.descriptor.credential_scopes():
                owner = by_scope.get(scope)
                if owner is not None:
                    raise RuntimeError(
                        f"duplicate credential_scope {scope!r} declared by both "
                        f"{owner!r} and {decl.module!r} — one scope has exactly one owner"
                    )
                by_scope[scope] = decl.module

            pkg = decl.descriptor.sandbox_package
            if pkg is not None:
                owner = by_env_key.get(pkg.version_env_key)
                if owner is not None:
                    raise RuntimeError(
                        f"duplicate sandbox_package.version_env_key {pkg.version_env_key!r} "
                        f"declared by both {owner!r} and {decl.module!r}"
                    )
                by_env_key[pkg.version_env_key] = decl.module

            declarations.append(decl)

    return declarations


def enumerate_sandbox_packages(project_root: Path | None = None) -> list[SandboxPackage]:
    """Sandbox packages from ``agent_harnesses[].sandbox_package`` plus the legacy section.

    The legacy top-level ``sandbox_packages`` array is still read for one cycle so a
    module that hasn't migrated keeps its CLI pin. A harness declaration wins over a
    legacy entry for the same ``version_env_key``.
    """
    packages: list[SandboxPackage] = []
    by_env_key: dict[str, str] = {}

    for decl in enumerate_harness_declarations(project_root):
        pkg = decl.descriptor.sandbox_package
        if pkg is not None:
            by_env_key[pkg.version_env_key] = decl.module
            packages.append(pkg)

    for module_dir in iter_enabled_module_dirs(project_root):
        for pkg in _parse_legacy_sandbox_packages(module_dir / "di.json", module_dir.name):
            prior = by_env_key.get(pkg.version_env_key)
            if prior is not None:
                if prior == module_dir.name:
                    # Same module declares both sections during migration — the
                    # agent_harnesses entry already covers it.
                    continue
                raise RuntimeError(
                    f"duplicate sandbox_packages.version_env_key {pkg.version_env_key!r} "
                    f"declared by both {prior!r} and {module_dir.name!r}"
                )
            by_env_key[pkg.version_env_key] = module_dir.name
            packages.append(pkg)

    return packages


def _parse_legacy_sandbox_packages(di_json: Path, module: str) -> list[SandboxPackage]:
    """Read the deprecated top-level ``sandbox_packages`` array. [] on absence/error."""
    try:
        data = json.loads(di_json.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    decls = data.get(LEGACY_SANDBOX_SECTION, [])
    if not isinstance(decls, list):
        return []
    out: list[SandboxPackage] = []
    for decl in decls:
        if not isinstance(decl, dict):
            continue
        harness = decl.get("provider") or decl.get("harness") or module
        try:
            out.append(SandboxPackage.from_declaration(decl, str(harness)))
        except ValueError as e:
            # Surface as a hard error so a typo doesn't silently drop an agent's pin.
            raise RuntimeError(f"Malformed {LEGACY_SANDBOX_SECTION} in {di_json}: {e}") from None
    return out
