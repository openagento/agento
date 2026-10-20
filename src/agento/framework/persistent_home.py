"""Persistent HOME path helpers shared by workspace builds and per-run HOME dirs."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .workspace_paths import BUILD_DIR


def state_dir(
    workspace_code: str,
    agent_view_code: str,
    *,
    build_root: str | Path = BUILD_DIR,
) -> Path:
    return Path(build_root) / workspace_code / agent_view_code / "state"


def ensure_state_dir(
    workspace_code: str,
    agent_view_code: str,
    persistent_paths: list[str],
    *,
    build_root: str | Path = BUILD_DIR,
) -> Path:
    """Create the per-agent_view persistent ``state/`` directory."""
    state = state_dir(workspace_code, agent_view_code, build_root=build_root)
    state.mkdir(parents=True, exist_ok=True)
    for rel in persistent_paths:
        (state / rel).mkdir(parents=True, exist_ok=True)
    return state


def link_persistent_paths(
    home_dir: Path,
    state_dir: Path,
    persistent_paths: list[str],
) -> None:
    """Symlink declared persistent HOME paths to shared per-agent_view state."""
    for rel in persistent_paths:
        home_target = home_dir / rel
        state_target = state_dir / rel
        home_target.parent.mkdir(parents=True, exist_ok=True)
        if home_target.is_symlink() or home_target.exists():
            if home_target.is_dir() and not home_target.is_symlink():
                shutil.rmtree(home_target)
            else:
                home_target.unlink()
        rel_target = os.path.relpath(state_target, home_target.parent)
        home_target.symlink_to(rel_target)


def copy_in_persistent_paths(home_dir: Path, state_dir: Path, persistent_paths: list[str]) -> None:
    """Copy the declared persistent HOME paths from ``state_dir`` into a run HOME as real
    files. A link into ``state/`` would not do: the runner gives a run uid its run dir only,
    never ``state/``, which every run of the agent_view shares (runner.md)."""
    for rel in persistent_paths:
        src, dst = state_dir / rel, home_dir / rel
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        elif dst.is_dir():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            # ponytail: a full copy per run; copy only the resumed session if state/ grows large.
            shutil.copytree(src, dst, symlinks=True, ignore=_links)
        elif src.is_file():
            shutil.copy2(src, dst)
        else:
            dst.mkdir()


def copy_back_persistent_paths(home_dir: Path, state_dir: Path, persistent_paths: list[str]) -> None:
    """Copy into ``state_dir`` each file the run added or changed. A file the run left
    alone keeps the mtime ``copy2`` gave it, so it never overwrites a newer write of a
    concurrent run. Links are skipped: the run wrote them, and ``state/`` must not follow
    one out. ponytail: two runs that change the same file — last writer wins."""
    for rel in persistent_paths:
        src = home_dir / rel
        if src.is_symlink():
            continue
        if src.is_file():
            _copy_if_newer(src, state_dir / rel)
            continue
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for name in files:
                path = Path(root, name)
                if not path.is_symlink():
                    _copy_if_newer(path, state_dir / rel / path.relative_to(src))


def _links(directory: str, names: list[str]) -> set[str]:
    return {n for n in names if os.path.islink(os.path.join(directory, n))}


def _copy_if_newer(src: Path, dst: Path) -> None:
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
