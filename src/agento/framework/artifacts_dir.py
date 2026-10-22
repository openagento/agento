"""Per-job artifacts directory management.

Each job gets its own artifacts directory under workspace/artifacts/.
It contains the copied config files + symlinks to build assets, and holds
any per-job outputs the agent or toolbox drops (screenshots, videos, session
scratch). Directories are created at job start and are **retained** afterwards —
they hold the run's artifacts for review. `prepare_artifacts_dir` wipes a dir
before reusing it; periodic cleanup of old run dirs is a ROADMAP item.
`cleanup_artifacts_dir` below is the retired removal path, kept for callers that
want it explicitly; nothing in the framework calls it.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
from pathlib import Path

from .workspace_paths import ARTIFACTS_DIR, BUILD_DIR

logger = logging.getLogger(__name__)


# A workspace / agent_view code as a run-dir segment: never `_…` (the root's own dirs), `.…`
# or a separator.
_RUN_DIR_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def build_artifacts_dir(workspace_code: str, agent_view_code: str, job_id: int | str) -> Path:
    """Build the artifacts directory path for a single job execution.

    A code is a path segment next to the artifacts root's own dirs (``_threads``,
    ``_fallback``), which start with ``_``. ``validate_code`` guards the write paths; this
    check is where every run dir is named, so a row no write path checked (a raw INSERT, a
    module's data patch) still cannot name one of them. It is wider than ``validate_code``
    (the toolbox's ``buildArtifactsDir`` keeps ``-`` and capitals too), so a view made by
    hand before this check keeps running.
    """
    for code in (workspace_code, agent_view_code):
        if not isinstance(code, str) or not _RUN_DIR_CODE.fullmatch(code):
            raise ValueError(f"code {code!r} cannot name a run dir: must match {_RUN_DIR_CODE.pattern}")
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


# The thread dirs (docs/architecture/threads.md). `_threads` starts with `_`, which no
# workspace code may (`validate_code`), so it never meets a run dir.
THREADS_DIRNAME = "_threads"
SHARED_LINK = "shared"
_THREAD_KEY = re.compile(r"[0-9a-f]{32}")
_OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def thread_dir(key: str) -> Path:
    """``<artifacts>/_threads/<key>``: the dir of the thread whose random key is ``key``."""
    if not _THREAD_KEY.fullmatch(key):
        raise ValueError("a thread key is 32 lowercase hex characters")
    return Path(ARTIFACTS_DIR) / THREADS_DIRNAME / key


def shared_dir(key: str) -> Path:
    """The dir every run of the thread reads and writes (``./shared`` in its run dir)."""
    return thread_dir(key) / SHARED_LINK


def downloads_dir(key: str) -> Path:
    """The dir the toolbox downloads the thread's files into; a run only reads it."""
    return thread_dir(key) / "downloads"


def _thread_subdir(parent_fd: int, name: str, mode: int, gid: int) -> int:
    """An open fd of the dir ``name`` in ``parent_fd``, made when missing. An existing entry
    must be a directory (not a symlink) that this process owns: it then gets ``mode`` and
    group ``gid`` (-1 keeps the group) on its own fd, so the inode checked is the inode
    changed (SEC-5)."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    try:
        fd = os.open(name, _OPEN_DIR, dir_fd=parent_fd)
    except OSError as exc:
        raise RuntimeError(f"thread dir component {name!r} is not a directory or is a symlink") from exc
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise RuntimeError(f"thread dir component {name!r} is not owned by this process")
        try:
            os.fchown(fd, -1, gid)
        except PermissionError as exc:
            raise RuntimeError(
                f"cannot give {name!r} group {gid}: this process is not in the thread group "
                "(the image adds `agent` to `agento_thread`; rebuild it)") from exc
        os.fchmod(fd, mode)  # after the group: a chown may clear the setgid bit
    except BaseException:
        os.close(fd)
        raise
    return fd


def prepare_thread_dir(key: str, run_dir: Path, *, gid: int | None = None) -> Path:
    """Make the thread's dirs and link ``<run_dir>/shared`` to its ``shared/``. Idempotent.

    ``_threads`` 0711 (traverse only), ``<key>`` 2750 and ``downloads`` 2750 (no run adds,
    renames or removes an entry: the toolbox writes there), ``shared`` 2770 (every run of the
    thread). The group is ``THREAD_GID``, which the runner gives to every run. ``gid`` is
    for tests only. A component that is a symlink or that another uid owns fails the run.
    An entry ``shared`` the build copied into the run dir gives way to the link.
    """
    from .runner.uids import THREAD_GID
    group = THREAD_GID if gid is None else gid
    path = thread_dir(key)
    fds = [os.open(ARTIFACTS_DIR, _OPEN_DIR)]
    try:
        fds.append(_thread_subdir(fds[-1], THREADS_DIRNAME, 0o711, -1))
        fds.append(_thread_subdir(fds[-1], key, 0o2750, group))
        for sub, mode in ((shared_dir(key), 0o2770), (downloads_dir(key), 0o2750)):
            os.close(_thread_subdir(fds[-1], sub.name, mode, group))
    finally:
        for fd in fds:
            os.close(fd)
    link = run_dir / SHARED_LINK
    if link.is_symlink() or link.is_file():
        link.unlink()
    elif link.exists():
        shutil.rmtree(link)
    link.symlink_to(shared_dir(key))
    return path


def cleanup_artifacts_dir(artifacts_dir: Path) -> None:
    """Remove the artifacts directory. NOT called by the framework any more.

    Run dirs are retained for review; a run dir is wiped by
    ``prepare_artifacts_dir`` when the same run number is reused.
    """
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
    run_id: str | None = None,
    capability_token: str | None = None,
    toolbox_url: str | None = None,
    harness: str | None = None,
    effective_model: str | None = None,
    effective_provider: str | None = None,
) -> None:
    """Thin bootstrap: copy small config files, symlink large readonly content.

    Files/dirs owned by registered WorkspaceAdapters are copied (so per-job
    runtime params can be injected). Everything else is symlinked.
    Dispatches runtime param injection to the harness's WorkspaceAdapter.
    """
    # No harness (a blank job / a view with none configured) owns no build files, so
    # everything but the universal copies is symlinked.
    foreign: set[str] = set()
    if harness is None:
        owned_files, owned_dirs = set(), set()
    else:
        from .harness import list_harnesses, owned_paths_for
        owned_files, owned_dirs = owned_paths_for(harness)
        # A job pinned to another harness than the view's copies the view's build. That
        # harness's files — its credential among them — must not reach this run.
        for other in list_harnesses():
            if other.descriptor.id != harness:
                foreign.update(*other.adapter.workspace_adapter.owned_paths())
        foreign -= owned_files | owned_dirs
    copy_files = owned_files | _UNIVERSAL_COPY_FILES

    for item in build_dir.iterdir():
        dest = artifacts_dir / item.name
        if item.name in foreign:
            continue
        if item.name in copy_files and item.is_file():
            shutil.copy2(item, dest)
        elif item.name in owned_dirs and item.is_dir():
            shutil.copytree(item, dest, symlinks=True)
        elif item.is_dir():
            dest.symlink_to(item.resolve())
        else:
            dest.symlink_to(item.resolve())

    # Inject runtime params via provider-specific WorkspaceAdapter
    if harness is not None:
        try:
            import inspect as _inspect

            from .harness import workspace_adapter_for
            writer = workspace_adapter_for(harness)
            kwargs: dict = {"job_id": job_id}
            # `effective_*` are the PER-RUN values (a `--model` override wins over
            # build-time config). A build-time expectation would otherwise fail a
            # legitimate override. Signature-aware so an adapter predating these keywords
            # is not handed an unknown one.
            # An EXPLICITLY NAMED override parameter, never `**kwargs`. A legacy adapter may
            # carry `**kwargs` purely for forward compatibility while still declaring
            # `job_id: int` — treating that as "understands a job-less run" would hand it a
            # `None` it renders into its config as the literal "None". So `**kwargs` is good
            # enough to RECEIVE an override alongside a real job id, but only a named
            # parameter admits the job-less call.
            declares_model = declares_provider = takes_kwargs = declares_run = declares_cap = False
            try:
                params = _inspect.signature(writer.inject_runtime_params).parameters
                takes_kwargs = any(
                    prm.kind is _inspect.Parameter.VAR_KEYWORD for prm in params.values()
                )
                declares_model = "effective_model" in params
                declares_provider = "effective_provider" in params
                # `run_id` names a job-less run so its desk is its own. Only a NAMED
                # parameter qualifies, for the same reason `**kwargs` does not qualify an
                # adapter for a `None` job id: an adapter that never heard of run scope
                # must not be handed it.
                declares_run = "run_id" in params
                # The capability is the run's toolbox credential. Named or `**kwargs`, like
                # the overrides: an adapter that cannot receive it keeps its baked config and
                # its toolbox session is refused with 401 — never silently widened.
                declares_cap = "capability_token" in params
                if capability_token and (declares_cap or takes_kwargs):
                    kwargs["capability_token"] = capability_token
                    kwargs["toolbox_url"] = toolbox_url
                if run_id and declares_run:
                    kwargs["run_id"] = run_id
                if effective_model and (declares_model or takes_kwargs):
                    kwargs["effective_model"] = effective_model
                if effective_provider and (declares_provider or takes_kwargs):
                    kwargs["effective_provider"] = effective_provider
            except (TypeError, ValueError):  # pragma: no cover - exotic callables
                pass
            # A `None` job_id (a string-id `agento run`) has no job scope to inject, so the
            # call is worth making only to apply a per-run override or the run scope — and
            # only to an adapter that named the keyword actually being supplied. Each half
            # is checked independently: an adapter that declares `effective_provider` alone
            # was previously excluded, because the gate keyed on `effective_model` only.
            explicit_override = bool(
                (effective_model and declares_model)
                or (effective_provider and declares_provider)
            )
            scopes_run = bool(run_id and declares_run)
            carries_cap = bool(capability_token and declares_cap)
            if job_id is None and not explicit_override and not scopes_run and not carries_cap:
                return
            writer.inject_runtime_params(artifacts_dir, **kwargs)
        except KeyError:
            logger.warning("No WorkspaceAdapter for harness %r, skipping runtime param injection", harness)
