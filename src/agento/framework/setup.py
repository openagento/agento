"""setup:upgrade orchestrator — Magento's ``bin/magento setup:upgrade`` equivalent.

Runs the full setup sequence in order:
1. Framework SQL migrations
2. Module SQL migrations (dependency order)
3. Data patches (topological order by ``require()``)
4. Cron installation
5. Module onboarding (interactive, skippable)
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pymysql

from .bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
from .crontab import (
    assemble,
    build_managed_block,
    collect_cron_jobs,
    extract_unmanaged,
    get_current_crontab,
    install_crontab,
)
from .data_patch import apply_patch, get_all_pending, resolve_patch_order
from .dependency_resolver import resolve_order, validate_dependencies
from .event_manager import get_event_manager
from .events import CrontabInstalledEvent, SetupBeforeEvent, SetupCompleteEvent
from .migrate import get_pending, migrate
from .module_loader import scan_modules
from .module_status import filter_enabled
from .module_validator import validate_module, validate_tool_namespace

FRAMEWORK_SQL_DIR = Path(__file__).parent / "sql"
FRAMEWORK_CRON_JSON = Path(__file__).parent / "cron.json"


@dataclass
class SetupResult:
    """Summary of what ``setup:upgrade`` applied (or would apply in dry-run)."""

    framework_migrations: list[str] = field(default_factory=list)
    module_migrations: dict[str, list[str]] = field(default_factory=dict)
    data_patches: dict[str, list[str]] = field(default_factory=dict)
    cron_changed: bool = False
    onboardings_run: list[str] = field(default_factory=list)
    onboardings_disabled: list[str] = field(default_factory=list)

    @property
    def has_work(self) -> bool:
        return bool(
            self.framework_migrations
            or self.module_migrations
            or self.data_patches
            or self.cron_changed
            or self.onboardings_run
            or self.onboardings_disabled
        )


class ModuleValidationError(Exception):
    """Raised when an enabled module's manifest fails validation during setup."""


def _validate_manifests(enabled, logger: logging.Logger) -> None:
    """Validate every enabled module's manifest. Raise (fail-fast) on any error.

    Runs before any migration so a broken manifest aborts setup:upgrade before
    the database is mutated. Per-module checks plus the cross-manifest
    tool-namespace check — the latter cannot be found module-by-module, and a
    duplicate tool name would silently reuse another module's name-keyed grant.
    """
    errors = {m.name: errs for m in enabled if (errs := validate_module(m.path))}
    for name, errs in validate_tool_namespace(
        (m.name, getattr(m, "tools", []) or []) for m in enabled
    ).items():
        errors.setdefault(name, []).extend(errs)
    if not errors:
        return
    for name, errs in sorted(errors.items()):
        for err in errs:
            logger.error("module '%s' invalid: %s", name, err)
    total = sum(len(e) for e in errors.values())
    raise ModuleValidationError(
        f"{total} manifest error(s) in {len(errors)} enabled module(s); "
        f"fix module.json (see log above) or run 'agento module:validate' before setup:upgrade"
    )


def setup_upgrade(
    conn: pymysql.Connection,
    logger: logging.Logger,
    *,
    dry_run: bool = False,
    skip_onboarding: bool = False,
    core_dir: str = CORE_MODULES_DIR,
    user_dir: str = USER_MODULES_DIR,
) -> SetupResult:
    """Run the full setup:upgrade sequence."""
    em = get_event_manager()
    result = SetupResult()

    em.dispatch("setup_upgrade_before", SetupBeforeEvent(dry_run=dry_run))

    # 0. Scan modules and validate enabled manifests up front — fail before
    #    mutating the database if any manifest is invalid.
    all_scanned = scan_modules(core_dir) + scan_modules(user_dir)
    enabled = filter_enabled(all_scanned)
    _validate_manifests(enabled, logger)
    validate_dependencies(enabled, all_scanned)
    manifests = resolve_order(enabled)

    # 1. Framework SQL migrations
    if dry_run:
        fw_pending = get_pending(conn, module="framework")
        result.framework_migrations = [v for v, _ in fw_pending]
    else:
        result.framework_migrations = migrate(conn, logger, module="framework")

    # 2. Module SQL migrations in dependency order

    for m in manifests:
        sql_dir = m.path / "sql"
        if not sql_dir.is_dir():
            continue
        if dry_run:
            pending = get_pending(conn, module=m.name, sql_dir=sql_dir)
            if pending:
                result.module_migrations[m.name] = [v for v, _ in pending]
        else:
            applied = migrate(conn, logger, module=m.name, sql_dir=sql_dir)
            if applied:
                result.module_migrations[m.name] = applied

    # 3. Data patches (topological order by require())
    pending_patches = get_all_pending(manifests, conn)
    if pending_patches:
        ordered = resolve_patch_order(pending_patches)
        if dry_run:
            for m, p in ordered:
                result.data_patches.setdefault(m.name, []).append(p["name"])
        else:
            for m, p in ordered:
                apply_patch(m, p, conn, logger)
                result.data_patches.setdefault(m.name, []).append(p["name"])

    # 4. Cron installation
    jobs = collect_cron_jobs(manifests, FRAMEWORK_CRON_JSON)
    current = get_current_crontab()
    unmanaged = extract_unmanaged(current)
    managed = build_managed_block(jobs)
    new_crontab = assemble(unmanaged, managed)
    result.cron_changed = install_crontab(new_crontab, dry_run=dry_run)
    if result.cron_changed and not dry_run:
        em.dispatch(
            "crontab_install_after",
            CrontabInstalledEvent(job_count=len(jobs)),
        )

    # 5. Module onboarding (interactive, skippable)
    if not dry_run and not skip_onboarding:
        from .bootstrap import get_module_config  # lazy: avoids circular with bootstrap
        from .cli.terminal import select
        from .dependency_resolver import get_transitive_dependents
        from .module_status import set_enabled
        from .onboarding import get_onboardings  # lazy: loaded after module bootstrap

        disabled_this_run: set[str] = set()

        for module_name, onboarding in get_onboardings().items():
            if module_name in disabled_this_run:
                continue
            if onboarding.is_complete(conn):
                continue

            choice = select(
                f"Module '{module_name}' needs onboarding: {onboarding.describe()}",
                ["Proceed with onboarding", "Skip (choose action)"],
            )

            if choice == 0:
                onboarding.run(conn, get_module_config(module_name), logger)

            while not onboarding.is_complete(conn):
                dependents = get_transitive_dependents(module_name, all_scanned)
                dep_label = f" (with dependents: {', '.join(dependents)})" if dependents else ""

                action = select(
                    f"Module '{module_name}' onboarding is not complete.",
                    [
                        "Retry",
                        f"Disable {module_name}{dep_label}",
                        "Quit",
                    ],
                )

                if action == 0:  # Retry
                    onboarding.run(conn, get_module_config(module_name), logger)
                elif action == 1:  # Disable
                    set_enabled(module_name, False)
                    disabled_this_run.add(module_name)
                    result.onboardings_disabled.append(module_name)
                    for dep in dependents:
                        set_enabled(dep, False)
                        disabled_this_run.add(dep)
                        result.onboardings_disabled.append(dep)
                    dep_msg = f" and dependents: {', '.join(dependents)}" if dependents else ""
                    logger.info("Disabled %s%s during onboarding", module_name, dep_msg)
                    break
                else:  # Quit
                    print("Setup aborted.")
                    sys.exit(1)

            if module_name not in disabled_this_run:
                result.onboardings_run.append(module_name)

    em.dispatch(
        "setup_upgrade_after",
        SetupCompleteEvent(result=result, dry_run=dry_run),
    )

    return result
