"""Per-run uids for the root runner server (docs/architecture/runner.md#per-run-uid).

Each run (an ``execute``, ``run`` or ``pty`` op) gets its own uid from the runner's fixed
range, with a primary group of the same number. The run dir is given to that uid with group
access (``2770``), so the trusted toolbox, which has every run gid as a supplementary group,
can write into it; another run is "other" and gets nothing. After the run, every process of
the uid is stopped and the dir goes back to ``agent``. A run has two kinds of other group:
``THREAD_GID``, so the runs of one thread share its ``shared/`` dir
(docs/architecture/threads.md), and ``AGENTO_RUN_GROUPS``, host groups an operator shares
with every run, so the agent can write a mounted host directory
(docs/deployment/docker-compose-override.md).

The server has no CAP_DAC_OVERRIDE: it never creates, writes or unlinks inside a run dir. It
only walks it, changes owners (CAP_CHOWN) and modes (CAP_FOWNER, CAP_FSETID).
"""
from __future__ import annotations

import contextlib
import errno
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from .. import workspace_paths

# The toolbox entrypoint repeats these two values; a test keeps them equal.
RUN_UID_BASE = 20000
RUN_UID_SLOTS = 256
KILL_ROUNDS = 50        # x KILL_PAUSE_S: a fork bomb of the uid gets 1 s, then the uid is kept
KILL_PAUSE_S = 0.02
DIR_MODE = 0o2770       # setgid: a new entry keeps the run's gid
# Every run's supplementary group: the thread dirs are `agent:THREAD_GID`, so the runs of
# one thread share its `shared/` (artifacts_dir.prepare_thread_dir). Below RUN_UID_BASE, so
# no run uid has it as its own. The sandbox Dockerfile names it `agento_thread`; a test keeps
# the two equal.
THREAD_GID = 19990


def uid_range(index: int) -> range:
    """The uids of runner ``index`` (1-based, from ``runner-<i>.sock``). Never overlaps
    another runner's range, whatever its ``max_procs``."""
    if index < 1:
        raise ValueError("runner index starts at 1")
    start = RUN_UID_BASE + (index - 1) * RUN_UID_SLOTS
    return range(start, start + RUN_UID_SLOTS)


def add_passwd_entries(run_uids: Iterable[int], passwd: str = "/etc/passwd") -> None:
    """Give every run uid a passwd entry. ``ssh`` stops with "No user exists for uid" when
    ``getpwuid()`` finds none. The home is ``/nonexistent``: ``ssh`` reads ``~/.ssh`` from
    it, not from ``$HOME``, and a run uid cannot create it, so no run plants an identity
    there for a later run. Idempotent: a container restart keeps the file."""
    with open(passwd, "r+") as f:
        text = f.read()
        have = {line.split(":")[2] for line in text.splitlines() if line.count(":") >= 2}
        new = "".join(f"run{uid}:x:{uid}:{uid}::/nonexistent:/usr/sbin/nologin\n"
                      for uid in run_uids if str(uid) not in have)
        if new:
            f.write(("\n" if text and not text.endswith("\n") else "") + new)


class UidPool:
    """The free uids of one runner. ``take`` gives ``None`` when none is free."""

    def __init__(self, uids: Iterable[int]):
        self._free = set(uids)
        self._lock = threading.Lock()

    def take(self) -> int | None:
        with self._lock:
            return self._free.pop() if self._free else None

    def give_back(self, uid: int) -> None:
        with self._lock:
            self._free.add(uid)


_OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
ROOTS = ("artifacts", ".tmp")


def _open_run_dir(path: str) -> int:
    """An open fd of the run dir ``path``, only when it is a directory strictly under
    ``<workspace>/artifacts/`` or ``<workspace>/.tmp/``. Each component is opened from
    ``/`` with ``O_NOFOLLOW``: a symlink anywhere on the path (the roots too) is refused,
    and nothing is resolved by path again after the check (SEC-5)."""
    names = [n for n in path.split("/") if n]
    base = [n for n in workspace_paths.BASE_WORKSPACE_DIR.split("/") if n]
    if (not path.startswith("/") or "." in names or ".." in names or len(names) < len(base) + 2
            or names[:len(base)] != base or names[len(base)] not in ROOTS):
        raise ValueError("the run dir is not under /workspace/artifacts or /workspace/.tmp")
    fd = os.open("/", _OPEN_DIR)
    try:
        for name in names:
            parent, fd = fd, -1
            try:
                fd = os.open(name, _OPEN_DIR, dir_fd=parent)
            finally:
                os.close(parent)
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        raise ValueError("the run dir is not a directory, or has a symlink on its path") from exc
    return fd


def run_dir(path: str) -> str:
    """``path`` (without empty components) when it passes the run-dir rule (``_open_run_dir``).
    The caller is the trusted backend, but a planted symlink must not move the owner change."""
    os.close(_open_run_dir(path))
    return "/" + "/".join(n for n in path.split("/") if n)


def _gone_ok(err: OSError) -> None:
    """``os.fwalk`` error: an entry that vanished is fine; any other error stops the walk."""
    if not isinstance(err, FileNotFoundError):
        raise err


_OPEN_ENTRY = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
_NOT_OPENED = (errno.ELOOP, errno.ENXIO, errno.EOPNOTSUPP)  # a symlink, a socket


def _own_entry(name: str, dirfd: int, uid: int, gid: int, retiring: int | None) -> None:
    """Change one entry through its own descriptor: the inode checked is the inode changed.
    A file with another hard link is skipped on the way in (SEC-5): that inode can also be
    outside the run dir, and its owner must not move. Not refused: pnpm, uv and ``git clone
    --local`` make hard links inside HOME. On the way back it is changed when the
    ``retiring`` uid (or its gid) holds it: only that run made it, and the uid must keep
    nothing for its next run (SEC-9). An inode of another run keeps its owner. A symlink and a socket are skipped: their owner grants nothing here, and a
    change by name after a check could reach a swapped entry."""
    try:
        fd = os.open(name, _OPEN_ENTRY, dir_fd=dirfd)  # CAP_DAC_READ_SEARCH: any mode
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno in _NOT_OPENED:
            return
        raise
    try:
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode) or st.st_nlink == 1 or retiring in (st.st_uid, st.st_gid):
            os.fchown(fd, uid, gid)
    finally:
        os.close(fd)


def own(path: str, uid: int, gid: int, *, for_run: bool, retiring: int | None = None) -> None:
    """Give the run dir ``path`` and everything in it to ``uid:gid``. The dir is opened again
    by the run-dir rule, and the walk works on descriptors only (``os.fwalk``, no link
    followed): a path swapped after the check never moves it out of the dir. Each entry is
    changed by ``_own_entry``; ``retiring`` is the run uid a give-back takes the dir from.
    ``for_run``: every directory gets ``2770``. File modes stay: a
    CLI checks some of them (ssh refuses a group-writable ``~/.ssh/config``), and the toolbox
    writes only files it creates.

    It runs only while no process of a run uid is alive in it: before the spawn (the dir is
    still ``agent``'s) and after the kill. An ``agent`` process may still change paths."""
    top = _open_run_dir(path)
    try:
        os.fchown(top, uid, gid)
        for _, dirnames, filenames, dirfd in os.fwalk(".", dir_fd=top, follow_symlinks=False,
                                                      onerror=_gone_ok):
            if for_run:
                os.fchmod(dirfd, DIR_MODE)  # after its chown: a chown clears the setgid bit
            # A sub dir is changed here, before the walk opens it, so its mode is set after
            # its owner.
            for name in dirnames + filenames:
                _own_entry(name, dirfd, uid, gid, retiring)
    finally:
        os.close(top)


def record(state_dir: str | None, uid: int, real: str | None) -> None:
    """Note (``real``) or forget (``None``) the run dir ``uid`` holds, in the runner's
    root-only state dir. A server that dies mid-run leaves the note, and ``reclaim`` reads it."""
    if not state_dir:
        return
    note = os.path.join(state_dir, str(uid))
    if real is None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(note)
        return
    with open(note, "w") as f:
        f.write(real)


def reclaim(state_dir: str, uid: int, gid: int) -> set[int]:
    """At start, before any run: every run dir a dead server left with a run uid goes back to
    ``uid:gid``. The uid's next run must not get that run's HOME (its credential). No run of
    this runner is alive: they died with its PID namespace. Returns the uids whose dir could
    not be given back: their note stays (for the operator) and they never go in the pool."""
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    kept = set()
    for note in Path(state_dir).iterdir():
        try:
            own(run_dir(note.read_text()), uid, gid, for_run=False, retiring=int(note.name))
        except (OSError, ValueError):
            kept.add(int(note.name))
            continue
        note.unlink()
    return kept


# Set once at boot by `set_shared_groups`: THREAD_GID and the operator's AGENTO_RUN_GROUPS.
_shared_groups: tuple[int, ...] = ()


def parse_shared_groups(value: str, agent_gid: int) -> tuple[int, ...]:
    """The gids of ``AGENTO_RUN_GROUPS`` ("1000,1003"). Refuses root's group, ``agent``'s (run
    dirs go back to it) and every run gid (one run would reach another's dir)."""
    gids = []
    for part in filter(None, (p.strip() for p in value.split(","))):
        if not part.isdigit():
            raise ValueError(f"AGENTO_RUN_GROUPS: {part!r} is not a gid")
        gid = int(part)
        if gid == 0 or gid == agent_gid or gid >= RUN_UID_BASE:
            raise ValueError(f"AGENTO_RUN_GROUPS: gid {gid} is refused (root, agent or a run gid)")
        if gid not in gids:
            gids.append(gid)
    return tuple(gids)


def set_shared_groups(gids: tuple[int, ...]) -> None:
    """The groups of every run: ``gids`` (``AGENTO_RUN_GROUPS``) and ``THREAD_GID``."""
    global _shared_groups
    _shared_groups = gids if THREAD_GID in gids else (*gids, THREAD_GID)


def as_uid(uid: int) -> dict:
    """``Popen`` arguments that start a child as ``uid``, in its own group plus the shared ones."""
    return {"user": uid, "group": uid, "extra_groups": list(_shared_groups), "umask": 0o002}


def drop_to(uid: int) -> None:
    """In a forked child, before ``exec``: become ``uid`` with only the shared groups."""
    os.setgroups(list(_shared_groups))
    os.setresgid(uid, uid, uid)
    os.setresuid(uid, uid, uid)
    os.umask(0o002)


# ``kill(-1, sig)`` as the run uid: every process that uid may signal, which is its own. Not
# procps ``kill``: it answers 1 for a usage error and for a sent signal alike.
_KILL = "import os, sys; os.kill(-1, int(sys.argv[1]))"


def signal_uid(uid: int, sig: int) -> bool:
    """Send ``sig`` to every process of ``uid``, as that uid: the worker, the harness CLI in
    its own group, and a descendant that left both. ``True`` if the kill ran (Linux counts
    a process it may not signal, so it says nothing about whether one of ``uid`` was there)."""
    return subprocess.run([sys.executable, "-I", "-S", "-c", _KILL, str(int(sig))], **as_uid(uid),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def processes_of(uid: int, proc: str = "/proc") -> list[int]:
    """The live (not zombie) pids whose real or saved uid is ``uid``. Read from the
    ``Uid:`` line: a non-dumpable process' ``/proc/<pid>`` is root-owned, its status is not."""
    pids = []
    for name in os.listdir(proc):  # an error here is no answer, never "none left"
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc, name, "status")) as f:
                fields = dict(line.split(":", 1) for line in f.read().splitlines() if ":" in line)
        except (FileNotFoundError, ProcessLookupError):
            continue  # gone; any other error goes to the caller (SEC-9)
        uids = fields.get("Uid", "").split()
        if fields.get("State", "").split()[:1] != ["Z"] and str(uid) in (uids[:1] + uids[2:3]):
            pids.append(int(name))
    return pids


def kill_all(uid: int) -> bool:
    """Stop every process of ``uid``. ``kill(-1)`` never answers ESRCH while other
    processes exist (Linux counts EPERM targets), so ``/proc`` says when it is done.
    ``False``: some are left after the bound, or ``/proc`` could not be read, and the uid
    must not be used again."""
    for _ in range(KILL_ROUNDS):
        signal_uid(uid, signal.SIGKILL)
        try:
            if not processes_of(uid):
                return True
        except OSError:
            pass  # a scan that did not finish is not "none left": try again, then give up
        time.sleep(KILL_PAUSE_S)
    return False
