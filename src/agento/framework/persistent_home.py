"""Persistent HOME path helpers shared by workspace builds and per-run HOME dirs."""
from __future__ import annotations

import filecmp
import logging
import os
import secrets
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from .workspace_paths import BUILD_DIR

logger = logging.getLogger(__name__)

# `session_file(root, session_id)` of a harness adapter: where the session lives under `root`.
SessionFinder = Callable[[Path, str], "Path | None"]


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
    """Create the per-agent_view persistent ``state/`` directory. A missing persistent path
    becomes a dir (``link_persistent_paths`` needs a target); one that exists, a file
    included, is left as it is."""
    state = state_dir(workspace_code, agent_view_code, build_root=build_root)
    state.mkdir(parents=True, exist_ok=True)
    for rel in persistent_paths:
        if not os.path.lexists(state / rel):
            (state / rel).mkdir(parents=True)
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


def copy_in_persistent_paths(
    home_dir: Path,
    state_dir: Path,
    persistent_paths: list[str],
    *,
    sessions: Iterable[Path] | None = None,
) -> None:
    """Give a run HOME its persistent paths as real files. A link into ``state/`` would not
    do: the runner gives a run uid its run dir only, never ``state/``, which every run of the
    agent_view shares (runner.md).

    ``sessions`` lists the session files (under ``state_dir``) the run resumes: only they,
    each with its sibling dir of the same stem, are copied, and nothing else is created —
    the harness CLI makes its own dirs and files. ``None`` (a harness with no
    ``session_file``) copies every persistent path whole, as before."""
    for rel in persistent_paths:
        if not _remove_entry(home_dir, rel):
            logger.warning("Run HOME %s: a parent of %s is a link, left as it is", home_dir, rel)
            continue
        if sessions is not None:
            continue
        src, dst = state_dir / rel, home_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True, ignore=_links)
        elif src.is_file():
            shutil.copy2(src, dst)
        else:
            dst.mkdir()
    for session in sessions or ():
        rel = _relative_under(Path(session), state_dir, persistent_paths)
        src = _no_link_path(state_dir, rel) if rel is not None else None
        if rel is None or src is None or not src.is_file():
            logger.debug("Session %s is not a file under %s, not copied", session, state_dir)
            continue
        dst = _no_link_path(home_dir, rel)
        if dst is None:
            # A link on the way (a linked parent kept above): never write through it (SEC-5).
            logger.warning("Run HOME %s: a parent of %s is a link, session not copied",
                           home_dir, rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        sidecar = src.parent / src.stem
        if sidecar.is_dir() and not sidecar.is_symlink():
            shutil.copytree(sidecar, dst.parent / src.stem, symlinks=True, ignore=_links,
                            dirs_exist_ok=True)


def session_relocations(
    finder: SessionFinder | None,
    home_dir: Path,
    state_dir: Path,
    session_ids: Iterable[str | None],
) -> dict[Path, Path]:
    """``{path in the run HOME: path in state/}`` (both relative) for each session the run
    holds under another path than ``state/`` does — the harness moved it into the bucket of
    the run's cwd (``move_session_into``). Copy-back writes it to its ``state/`` path, so
    ``state/`` keeps one copy per session. Asked of ``session_file`` on both roots, so no
    mapping is kept between copy-in and copy-back."""
    moved: dict[Path, Path] = {}
    if finder is None:
        return moved
    for sid in dict.fromkeys(s for s in session_ids if s):
        try:
            now, orig = finder(home_dir, sid), finder(state_dir, sid)
        except Exception as exc:
            logger.debug("session_file failed for session %s: %s", sid, type(exc).__name__)
            continue
        if now is None or orig is None:
            continue
        now_rel, orig_rel = _relative_to(now, home_dir), _relative_to(orig, state_dir)
        if now_rel is None or orig_rel is None or now_rel == orig_rel:
            continue
        now_path, orig_path = _no_link_path(home_dir, now_rel), _no_link_path(state_dir, orig_rel)
        if now_path is not None and orig_path is not None and now_path.is_file() and orig_path.is_file():
            moved[now_rel] = orig_rel
    return moved


def state_name_index(state_dir: Path, persistent_paths: list[str]) -> dict[str, list[Path]]:
    """``{file name: [paths relative to state_dir]}`` over every persistent path, for
    ``copy_back_persistent_paths(known_names=…)``. Build it once per ``state/``."""
    index: dict[str, list[Path]] = {}
    for rel in persistent_paths:
        for path in _real_files(state_dir, rel):
            index.setdefault(path.name, []).append(path.relative_to(state_dir))
    return index


def copy_back_persistent_paths(
    home_dir: Path,
    state_dir: Path,
    persistent_paths: list[str],
    *,
    relocate: Mapping[Path, Path] | None = None,
    known_names: dict[str, list[Path]] | None = None,
) -> list[Path]:
    """Copy into ``state_dir`` each file the run added or changed, and return the run-HOME
    paths it could not save (conflicts). An I/O error raises.

    A file the run left alone keeps the mtime ``copy2`` gave it, so it never overwrites a
    newer write of a concurrent run (ponytail: two runs that change the same file — last
    writer wins). Every write is atomic. Nothing is read through a link: a path with a link
    in any component, from ``home_dir`` down, is skipped — the run wrote it, and ``state/``
    must not follow one out.

    ``relocate`` (``session_relocations``) sends a moved session, and its sibling dir of the
    same stem, back to its ``state/`` path. ``known_names`` (``state_name_index``) is for a
    run dir whose sessions are not known: a file whose name ``state/`` already holds (at
    this path or another) is a copy when exactly one such file has the same bytes, and a
    conflict otherwise — saving it would make a second copy of a session or drop the run's
    own bytes. Each file saved is added to ``known_names`` at once, so a later file of the
    same name — in this call or the next — is checked against it."""
    targets = _relocation_targets(relocate or {})
    conflicts: list[Path] = []
    for rel in persistent_paths:
        for src in _real_files(home_dir, rel):
            home_rel = src.relative_to(home_dir)
            dst_rel = targets(home_rel)
            dst = state_dir / dst_rel
            if known_names is not None and dst_rel == home_rel:
                same = known_names.get(src.name, ())
                if len(same) == 1 and filecmp.cmp(src, state_dir / same[0], shallow=False):
                    continue
                if same:
                    conflicts.append(src)
                    continue
                if _copy_if_newer(src, dst):
                    known_names.setdefault(src.name, []).append(dst_rel)
                else:
                    conflicts.append(src)
                continue
            if not _copy_if_newer(src, dst):
                conflicts.append(src)
    return conflicts


def drop_persistent_paths(home_dir: Path, persistent_paths: list[str]) -> None:
    """Remove the persistent paths from a run HOME (after a copy-back saved them). A link is
    unlinked, never followed; a path under a linked parent is left alone."""
    for rel in persistent_paths:
        _remove_entry(home_dir, rel)


def _remove_entry(root: Path, rel: str | Path) -> bool:
    """Remove ``root/rel``: a link or file is unlinked, a real dir removed. ``False`` (and
    nothing removed) when a parent component is a link."""
    parent = _no_link_path(root, Path(rel).parent)
    if parent is None:
        return False
    path = parent / Path(rel).name
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    return True


def _no_link_path(root: Path, rel: Path) -> Path | None:
    """``root/rel`` when no component below ``root``, the last one included, is a link (and
    ``rel`` has no ``..``); else ``None``. Every read of a path the run controls goes
    through it (SEC-5)."""
    path = root
    for part in Path(rel).parts:
        if part in ("..", "/") or Path(part).is_absolute():
            return None
        path = path / part
        if path.is_symlink():
            return None
    return path


def _real_files(root: Path, rel: str) -> Iterable[Path]:
    """Every regular file at or under ``root/rel``, never through a link."""
    top = _no_link_path(root, Path(rel))
    if top is None:
        return
    if top.is_file():
        yield top
        return
    for current, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
        for name in files:
            path = Path(current, name)
            if not path.is_symlink() and path.is_file():
                yield path


def _relocation_targets(relocate: Mapping[Path, Path]) -> Callable[[Path], Path]:
    """Map a run-HOME relative path to its ``state/`` relative path."""
    sidecars = {now.parent / now.stem: orig.parent / orig.stem for now, orig in relocate.items()}

    def target(rel: Path) -> Path:
        if rel in relocate:
            return relocate[rel]
        for now, orig in sidecars.items():
            if rel.is_relative_to(now):
                return orig / rel.relative_to(now)
        return rel

    return target


def _relative_under(path: Path, root: Path, persistent_paths: list[str]) -> Path | None:
    """``path`` relative to ``root`` when it lies strictly under one persistent path."""
    rel = _relative_to(path, root)
    if rel is None:
        return None
    for p in persistent_paths:
        if rel != Path(p) and rel.is_relative_to(p):
            return rel
    return None


def _relative_to(path: Path, root: Path) -> Path | None:
    try:
        return Path(path).relative_to(root)
    except ValueError:
        return None


def _links(directory: str, names: list[str]) -> set[str]:
    return {n for n in names if os.path.islink(os.path.join(directory, n))}


def _copy_if_newer(src: Path, dst: Path) -> bool:
    """Copy ``src`` over an older ``dst``, atomically. ``False`` when ``dst`` is a dir that
    is not empty (a conflict); an empty one (``ensure_state_dir`` makes a dir at a file's
    path) is replaced."""
    if dst.is_dir() and not dst.is_symlink():
        if any(dst.iterdir()):
            return False
        dst.rmdir()
    elif dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{secrets.token_hex(4)}.tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return True
