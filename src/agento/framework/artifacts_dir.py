"""Per-job artifacts directory management.

Each job gets its own artifacts directory under workspace/artifacts/.
It contains the copied config files + symlinks to build assets, and holds
any per-job outputs the agent or toolbox drops (screenshots, videos, session
scratch). Directories are created at job start; on clean completion they are
removed, but crashed jobs leave their artifacts dir behind for inspection.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from agento.framework.workspace_paths import ARTIFACTS_DIR, BUILD_DIR

logger = logging.getLogger(__name__)


def build_artifacts_dir(workspace_code: str, agent_view_code: str, job_id: int | str) -> Path:
    """Build the artifacts directory path for a single job execution."""
    return Path(ARTIFACTS_DIR) / workspace_code / agent_view_code / str(job_id)


def prepare_artifacts_dir(artifacts_dir: Path) -> None:
    """Create the artifacts directory tree, cleaning any stale contents from prior attempts."""
    if artifacts_dir.exists():
        try:
            shutil.rmtree(artifacts_dir)
        except PermissionError as e:
            offender = Path(e.filename) if e.filename else artifacts_dir
            try:
                st = offender.stat()
                logger.error(
                    "PermissionError cleaning artifacts dir %s: cannot remove %s "
                    "(owner uid=%d gid=%d, mode=%o). Process running as uid=%d gid=%d. "
                    "If toolbox container ran as root before the fix, run "
                    "`sudo chown -R $(id -u):$(id -g) workspace/artifacts` on the host.",
                    artifacts_dir, offender, st.st_uid, st.st_gid, st.st_mode & 0o777,
                    os.getuid(), os.getgid(),
                )
            except OSError:
                logger.error(
                    "PermissionError cleaning %s (could not stat %s)",
                    artifacts_dir, offender,
                )
            raise
    artifacts_dir.mkdir(parents=True, exist_ok=True)


def cleanup_artifacts_dir(artifacts_dir: Path) -> None:
    """Remove the artifacts directory after successful job completion."""
    try:
        if artifacts_dir.exists():
            shutil.rmtree(artifacts_dir)
            logger.debug("Cleaned up artifacts dir %s", artifacts_dir)
    except Exception:
        logger.warning("Failed to clean up artifacts dir %s", artifacts_dir, exc_info=True)


def get_current_build_dir(workspace_code: str, agent_view_code: str) -> Path | None:
    """Return the current build directory if the symlink exists and target is valid."""
    current_link = Path(BUILD_DIR) / workspace_code / agent_view_code / "current"
    if current_link.is_symlink():
        target = current_link.resolve()
        if target.is_dir():
            return target
    return None


# Top-level build files copied (not symlinked) into each per-run artifacts dir so per-job edits stay
# isolated: the agent-agnostic instruction files, plus `.gitconfig` — copied so a run-time `git config`
# write stays private to the run instead of following a symlink back into the shared, persistent build
# dir (which would corrupt the identity for future runs).
_UNIVERSAL_COPY_FILES = {"CLAUDE.md", "AGENTS.md", "SOUL.md", ".gitconfig"}


def copy_build_to_artifacts_dir(
    build_dir: Path,
    artifacts_dir: Path,
    *,
    job_id: int | None = None,
    harness: str | None = None,
) -> None:
    """Thin bootstrap: copy small config files, symlink large readonly content.

    Files/dirs owned by registered WorkspaceAdapters are copied (so per-job
    runtime params can be injected). Everything else is symlinked.
    Dispatches runtime param injection to the harness's WorkspaceAdapter.
    """
    # No harness (a blank job / a view with none configured) owns no build files, so
    # everything but the universal copies is symlinked.
    if harness is None:
        owned_files, owned_dirs = set(), set()
    else:
        from agento.framework.harness import owned_paths_for
        owned_files, owned_dirs = owned_paths_for(harness)
    copy_files = owned_files | _UNIVERSAL_COPY_FILES

    for item in build_dir.iterdir():
        dest = artifacts_dir / item.name
        if item.name in copy_files and item.is_file():
            shutil.copy2(item, dest)
        elif item.name in owned_dirs and item.is_dir():
            shutil.copytree(item, dest, symlinks=True)
        elif item.is_dir():
            dest.symlink_to(item.resolve())
        else:
            dest.symlink_to(item.resolve())

    # Inject runtime params via provider-specific WorkspaceAdapter
    if job_id is not None and harness is not None:
        try:
            from agento.framework.harness import workspace_adapter_for
            writer = workspace_adapter_for(harness)
            writer.inject_runtime_params(artifacts_dir, job_id=job_id)
        except KeyError:
            logger.warning("No WorkspaceAdapter for harness %r, skipping runtime param injection", harness)
