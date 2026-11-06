"""Retention of run dirs and thread dirs (``artifacts:prune``, docs/architecture/threads.md).

A run dir (``<artifacts>/<ws>/<view>/<job>``) goes when its job ended (a terminal status)
before ``cutoff``; a dir with no job row goes by its mtime. Before it goes, what the
run left in its persistent HOME paths is copied back into the view's ``state/``, the
session store of record, which retention never touches. A dir whose content ``state/``
cannot take is kept. A thread dir (``<artifacts>/_threads/<key>``) goes when its thread
has no open job and its last job ended before ``cutoff``, under the thread row lock,
so no job joins the thread while its dir goes. A run dir of a job goes under that job's row
lock, so no retry claims it while it goes. Nothing here follows a link.
"""
from __future__ import annotations

import logging
import os
import shutil
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .artifacts_dir import _RUN_DIR_CODE, _THREAD_KEY, THREADS_DIRNAME
from .job_models import OPEN_STATUSES, TERMINAL_STATUSES
from .persistent_home import copy_back_persistent_paths, session_relocations, state_name_index
from .workspace_paths import ARTIFACTS_DIR

logger = logging.getLogger(__name__)

_BATCH = 500


@dataclass
class PruneReport:
    """Counters only: the paths go to ``on_file`` as they are walked (CODE-8)."""

    run_dirs: int = 0
    thread_dirs: int = 0
    bytes: int = 0
    kept: Counter[str] = field(default_factory=Counter)


def prune_run_dirs(
    conn,
    *,
    cutoff: datetime,
    dry_run: bool,
    report: PruneReport,
    on_file: Callable[[Path], None] | None = None,
    on_kept: Callable[[Path, str], None] | None = None,
    root: Path | None = None,
) -> None:
    """Remove the run dirs of jobs that ended before ``cutoff`` (naive UTC, like
    ``job.finished_at``)."""
    # Bounded memory (CODE-8): candidates are walked in batches of _BATCH, and the name
    # index of a view's state/ lives only while that view's dirs are walked.
    indexes: dict[Path, dict[str, list[Path]]] = {}
    current_view: tuple[str, str] | None = None
    for batch in _batches(_run_dir_candidates(Path(root or ARTIFACTS_DIR)), _BATCH):
        rows = _job_rows(conn, [int(path.name) for _, _, path in batch if path.name.isdigit()])
        for ws, view, path in batch:
            if (ws, view) != current_view:
                indexes.clear()
                current_view = (ws, view)
            row = rows.get(int(path.name)) if path.name.isdigit() else None
            if not _expired(row, path, cutoff, report, on_kept):
                continue
            if row is None:
                # No row, and an id is never reused, so no job can claim this dir.
                _prune_run_dir(ws, view, path, None, indexes, dry_run, report, on_file, on_kept)
                continue
            # The job row lock blocks a retry or a claim of the job while its dir goes; the
            # locking read sees the row as committed now, not the batch's earlier read.
            try:
                locked = _job_rows(conn, [row["id"]], lock=True).get(row["id"])
                if _expired(locked, path, cutoff, report, on_kept) and locked is not None:
                    _prune_run_dir(ws, view, path, locked, indexes, dry_run, report, on_file,
                                   on_kept)
            finally:
                conn.rollback()   # nothing written: only releases the row lock


def prune_thread_dirs(
    conn,
    *,
    cutoff: datetime,
    dry_run: bool,
    report: PruneReport,
    on_file: Callable[[Path], None] | None = None,
    on_kept: Callable[[Path, str], None] | None = None,
    root: Path | None = None,
) -> None:
    """Remove the dirs of threads with no open job whose last job ended before ``cutoff``
    (naive UTC). ``conn`` must be READ COMMITTED: the job read after the lock wait
    must see a job that a publisher committed while it waited (REPEATABLE READ answers from
    the snapshot of the first read)."""
    with conn.cursor() as cur:
        cur.execute("SELECT @@transaction_isolation AS iso")
        iso = (cur.fetchone() or {}).get("iso")
    if iso != "READ-COMMITTED":
        raise RuntimeError(f"prune_thread_dirs needs a READ COMMITTED connection, got {iso!r}")
    threads = Path(root or ARTIFACTS_DIR) / THREADS_DIRNAME
    if threads.is_symlink() or not threads.is_dir():
        return
    for path in _real_dirs(threads):
        if not _THREAD_KEY.fullmatch(path.name):
            continue
        # One transaction per thread. The row lock blocks a claim and an insert of the
        # thread (`threads.lock_thread`, `join_thread`, the `fk_job_thread` check) while the
        # dir goes; only the thread row is locked, so the job -> thread order holds.
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM thread WHERE `key` = %s FOR UPDATE", (path.name,))
                thread = cur.fetchone()
                if thread is None:
                    old = _mtime(path) < cutoff
                else:
                    cur.execute(
                        "SELECT COALESCE(SUM(status IN %s), 0) AS open_jobs, "
                        "MAX(finished_at) AS last_finished FROM job WHERE thread_id = %s",
                        (OPEN_STATUSES, thread["id"]),
                    )
                    agg = cur.fetchone()
                    if agg["open_jobs"]:
                        _keep(report, on_kept, path, "open")
                    old = not agg["open_jobs"] and (
                        agg["last_finished"] is None or agg["last_finished"] < cutoff)
                if old and _remove_tree(path, dry_run=dry_run, report=report, on_file=on_file,
                                        on_kept=on_kept):
                    report.thread_dirs += 1
        finally:
            if dry_run:
                conn.rollback()
            else:
                conn.commit()


def _expired(row: dict | None, path: Path, cutoff: datetime, report: PruneReport,
             on_kept: Callable[[Path, str], None] | None) -> bool:
    """Whether the run dir may go: its job ended before ``cutoff`` or, with no row, the dir
    is older than it. An open job is reported as kept."""
    if row is None:
        return _mtime(path) < cutoff
    if row["status"] in OPEN_STATUSES:
        _keep(report, on_kept, path, "open")
        return False
    return (row["status"] in TERMINAL_STATUSES and row["finished_at"] is not None
            and row["finished_at"] < cutoff)


def _prune_run_dir(ws: str, view: str, path: Path, row: dict | None,
                   indexes: dict[Path, dict[str, list[Path]]], dry_run: bool,
                   report: PruneReport, on_file: Callable[[Path], None] | None,
                   on_kept: Callable[[Path, str], None] | None) -> None:
    # A dry run checks the harness too (no copy-back), so it reports the same kept dirs.
    paths = _paths_to_save(path, row, report, on_kept)
    if paths is None or (not dry_run and not _save_sessions(ws, view, path, row, paths, indexes,
                                                            report, on_kept)):
        return
    if _remove_tree(path, dry_run=dry_run, report=report, on_file=on_file, on_kept=on_kept):
        report.run_dirs += 1


def _batches(items: Iterator[tuple[str, str, Path]], size: int
             ) -> Iterator[list[tuple[str, str, Path]]]:
    batch: list[tuple[str, str, Path]] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _mtime(path: Path) -> datetime:
    """The mtime of ``path`` itself (never a link's target), as naive UTC."""
    return datetime.fromtimestamp(path.lstat().st_mtime, UTC).replace(tzinfo=None)


def _run_dir_candidates(root: Path) -> Iterator[tuple[str, str, Path]]:
    """``(ws, view, run dir)`` for every real dir ``root/<ws>/<view>/<name>`` whose codes
    can name a run dir (``build_artifacts_dir``); ``_threads``, ``_fallback`` never can."""
    for ws in _real_dirs(root):
        if not _RUN_DIR_CODE.fullmatch(ws.name):
            continue
        for view in _real_dirs(ws):
            if _RUN_DIR_CODE.fullmatch(view.name):
                for run in _real_dirs(view):
                    yield ws.name, view.name, run


def _real_dirs(path: Path) -> Iterator[Path]:
    """The real (never linked) child dirs of ``path``, streamed: no list of a dir's entries
    is built, so memory does not grow with the number of run or thread dirs (CODE-8)."""
    if path.is_symlink() or not path.is_dir():
        return
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                yield Path(entry.path)


def _job_rows(conn, ids: list[int], *, lock: bool = False) -> dict[int, dict]:
    """The rows of ``ids``. ``lock`` takes the row lock of each ``j`` row only (never of the
    resumed job), so the prune holds one job row at a time."""
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.id, j.status, j.finished_at, j.agent_type, j.session_id, "
            "r.session_id AS resumed_session_id FROM job j "
            "LEFT JOIN job r ON r.id = j.resume_job_id "
            f"WHERE j.id IN ({', '.join(['%s'] * len(ids))})"
            + (" FOR UPDATE OF j" if lock else ""),
            tuple(ids),
        )
        return {row["id"]: row for row in cur.fetchall()}


def _paths_to_save(path: Path, row: dict | None, report: PruneReport,
                   on_kept: Callable[[Path, str], None] | None) -> list[str] | None:
    """The persistent HOME paths to copy back from the run dir. ``None`` keeps the dir."""
    from .harness import find_harness, list_harnesses, persistent_home_paths_for

    harness = row["agent_type"] if row is not None else None
    if harness is not None and find_harness(harness) is None:
        # A disabled harness: its persistent paths are unknown, so nothing can be saved.
        _keep(report, on_kept, path, "unknown_harness")
        return None
    if harness is not None:
        return persistent_home_paths_for(harness)
    registered = list_harnesses()
    if not registered:
        # No registry (bootstrap failed): the paths to save are unknown.
        _keep(report, on_kept, path, "unknown_harness")
        return None
    return sorted({p for h in registered for p in persistent_home_paths_for(h.descriptor.id)})


def _save_sessions(ws: str, view: str, path: Path, row: dict | None, paths: list[str],
                   indexes: dict[Path, dict[str, list[Path]]], report: PruneReport,
                   on_kept: Callable[[Path, str], None] | None) -> bool:
    """Copy back what the run left in its persistent HOME ``paths``. ``False`` keeps the dir."""
    from .run_preparation import session_finder, state_root_for_codes

    harness = row["agent_type"] if row is not None else None
    if not paths:
        return True
    try:
        state = state_root_for_codes(ws, view, paths)
        if harness is None:
            # No job row, so no session id: a file whose name state/ holds elsewhere is a
            # copy or a conflict, never a second copy. Copy-back adds each file it saves to
            # the index, so it stays current, also after a partial save.
            if state not in indexes:
                indexes[state] = state_name_index(state, paths)
            conflicts = copy_back_persistent_paths(path, state, paths, known_names=indexes[state])
        else:
            try:
                relocate = session_relocations(session_finder(harness), path, state,
                                               (row["session_id"], row["resumed_session_id"]))
                conflicts = copy_back_persistent_paths(path, state, paths, relocate=relocate)
            finally:
                # This save did not update the index: whatever it wrote, even in part, the
                # next row-less dir of the view gets a fresh one.
                indexes.pop(state, None)
    except OSError as exc:
        logger.warning("Run dir %s kept: copy-back failed (%s)", path, type(exc).__name__)
        _keep(report, on_kept, path, "copy_back_failed")
        return False
    if conflicts:
        logger.warning("Run dir %s kept: state/ cannot take %s", path,
                       ", ".join(str(c.relative_to(path)) for c in conflicts))
        _keep(report, on_kept, path, "unsaved")
        return False
    return True


def _remove_tree(path: Path, *, dry_run: bool, report: PruneReport,
                 on_file: Callable[[Path], None] | None,
                 on_kept: Callable[[Path, str], None] | None) -> bool:
    """Count and report each file or link under ``path`` (never through a link), then remove
    the tree. ``False`` (the dir kept) on an ``OSError`` — a dir a run uid still owns."""
    try:
        for current, dirs, files in os.walk(path, topdown=False, followlinks=False):
            for name in (*files, *(d for d in dirs if os.path.islink(os.path.join(current, d)))):
                entry = Path(current, name)
                report.bytes += entry.lstat().st_size
                if on_file is not None:
                    on_file(entry)
        if not dry_run:
            shutil.rmtree(path)
    except OSError as exc:
        logger.warning("Dir %s kept: %s", path, type(exc).__name__)
        _keep(report, on_kept, path, "permission")
        return False
    return True


def _keep(report: PruneReport, on_kept: Callable[[Path, str], None] | None, path: Path,
          reason: str) -> None:
    report.kept[reason] += 1
    if on_kept is not None:
        on_kept(path, reason)
