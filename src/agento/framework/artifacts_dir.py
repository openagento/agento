"""Per-job artifacts directory management.

Each job gets its own artifacts directory under workspace/artifacts/.
It contains the copied config files + symlinks to build assets, and holds
any per-job outputs the agent or toolbox drops (screenshots, videos, session
scratch). Directories are created at job start and are **retained** afterwards —
they hold the run's artifacts for review. `prepare_artifacts_dir` wipes a dir
before reusing it; the nightly `artifacts:prune` removes old run and thread dirs
(`artifacts_retention.py`).
`cleanup_artifacts_dir` below is the retired removal path, kept for callers that
want it explicitly; nothing in the framework calls it.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import stat
from pathlib import Path

from .workspace_paths import ARTIFACTS_DIR, BASE_WORKSPACE_DIR, BUILD_DIR, UPLOADS_DIRNAME

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


# One path component: never a separator, never a control character, never `.`/`..`, never a
# leading dot. It is a rule of EXCLUSION, not an allow-list: the name a user gave a file is
# sanitized where it is staged (`safe_name`), and that sanitizer keeps spaces and letters that
# are not ASCII. An allow-list here would pass staging and fail the run (BUG).
_SEPARATORS = ("/", "\\", "\0")

ATTACHMENTS_DIRNAME = "attachments"


class AttachmentError(RuntimeError):
    """A job's `attachments` descriptor cannot be delivered. The run fails loudly."""


def _component(value: object, what: str) -> str:
    """A descriptor field that becomes ONE directory or file name, or nothing."""
    bad = (not isinstance(value, str) or not value or len(value.encode()) > 255
           or value in (".", "..") or value.startswith(".")
           or any(sep in value for sep in _SEPARATORS)
           or any(ch < " " or ch == "\x7f" for ch in value))
    if bad:
        raise AttachmentError(f"attachment {what} is not a single path component")
    assert isinstance(value, str)
    return value


def parse_attachments(raw: str | None) -> tuple[str, list[tuple[str, str]]] | None:
    """``(key, [(from, as)])`` of a job's descriptor, or ``None`` when it has none.

    `from` is still the RELATIVE path the descriptor carries: the file behind it is only
    resolved when it is about to be copied (`_source_path`), so a delivery that is already
    done needs no staging file at all.

    The descriptor is vendor-free and self-contained — the framework never asks the module
    that wrote it what it means. Every name is validated here, BEFORE a byte is read, and a
    bad one fails the run: a `key`/`as` must be one path component, and a `from` must be a
    relative path under the workspace's ``uploads/`` (SEC-5).
    """
    if raw is None or raw == "":
        return None
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise AttachmentError("attachments is not JSON") from exc
    if not isinstance(doc, dict):
        raise AttachmentError("attachments is not an object")
    key = _component(doc.get("key"), "key")
    files = doc.get("files")
    if not isinstance(files, list) or not files:
        raise AttachmentError("attachments lists no file")
    out: list[tuple[str, str]] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise AttachmentError("an attachment entry is not an object")
        name = _component(entry.get("as"), "name")
        source = entry.get("from")
        if not isinstance(source, str) or not source:
            raise AttachmentError("an attachment has no source")
        if ".." in Path(source).parts or Path(source).is_absolute():
            raise AttachmentError("an attachment source leaves the workspace")
        out.append((source, name))
    return key, out


def _source_path(source: str) -> str:
    """The real file a `from` names, or raise. Asked only when a file is about to be COPIED:
    a delivery that is already done never looks at staging, which by then may be swept."""
    root = Path(BASE_WORKSPACE_DIR) / UPLOADS_DIRNAME
    path = Path(BASE_WORKSPACE_DIR) / source
    # The ROOT is checked before anything is resolved against it: `resolve()` would follow a
    # planted `uploads` link and then happily confirm that the file is "under uploads" (SEC-5).
    if root.is_symlink() or not root.is_dir():
        raise AttachmentError("the uploads root is not a directory")
    # `strict=False` would accept a dangling link; the file must be there and real.
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AttachmentError("attachment source missing") from exc
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise AttachmentError("an attachment source is not an uploaded file")
    return str(path)


def _is_delivered(root_fd: int, key: str, files: list[tuple[str, str]]) -> bool:
    """True when ``<root_fd>/<key>`` is a real dir holding every name as a real file.

    Every stat is relative to an fd and refuses a symlink, so a planted link named ``<key>``
    is never mistaken for a finished delivery (SEC-5). Completeness is checked HERE and
    nowhere else: a dir that merely exists is not a delivery (BUG).
    """
    try:
        fd = os.open(key, _OPEN_DIR, dir_fd=root_fd)
    except OSError:
        return False
    try:
        for _, name in files:
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                return False
            if not stat.S_ISREG(st.st_mode):
                return False
    finally:
        os.close(fd)
    return True


def attachments_delivered(thread_key: str | None, raw: str | None) -> bool:
    """True when the thread already holds every file this descriptor names.

    The one answer to "may the staged sources go?". Job state is NOT that answer: a run can
    reach a terminal state by failing DURING preparation, before the copy, and a retry would
    then find the only copy gone (BUG). What the thread holds is the fact.
    """
    parsed = parse_attachments(raw)
    if parsed is None:
        return True                       # nothing to deliver, nothing to keep
    if thread_key is None:
        return False                      # delivered under a run dir we cannot name here
    key, files = parsed
    try:
        fd = os.open(shared_dir(thread_key) / ATTACHMENTS_DIRNAME, _OPEN_DIR)
    except OSError:
        return False
    try:
        return _is_delivered(fd, key, files)
    finally:
        os.close(fd)


def _remove_at(parent_fd: int, name: str) -> None:
    """Remove a flat dir of regular files, relative to ``parent_fd``. Never follows a link."""
    try:
        fd = os.open(name, _OPEN_DIR, dir_fd=parent_fd)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=parent_fd)
        return
    try:
        for entry in os.listdir(fd):
            with contextlib.suppress(OSError):
                os.unlink(entry, dir_fd=fd)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        os.rmdir(name, dir_fd=parent_fd)


def _copy_into(dir_fd: int, name: str, source: str, group: int) -> None:
    """One file into ``dir_fd``, created exclusively and never through a link."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o640, dir_fd=dir_fd)
    try:
        with open(source, "rb") as src, os.fdopen(os.dup(fd), "wb") as dst:
            # copyfileobj streams through a fixed buffer: the memory is the buffer, not the file.
            shutil.copyfileobj(src, dst)
        os.fchown(fd, -1, group)
        os.fchmod(fd, 0o640)
    finally:
        os.close(fd)


def deliver_attachments(raw: str | None, shared: Path, *, gid: int | None = None) -> None:
    """Put a job's attachments at ``<shared>/attachments/<key>/<as>``. Idempotent.

    Called by run preparation, which owns the thread dirs. Three orders matter:

    1. **The delivered dir decides.** A dir that already holds every name is done, and the
       sources are not read at all — that is what makes a retry work, and what lets a
       regenerated turn reuse files whose staging copies are long gone. A dir that holds only
       SOME of them is not a delivery and never ends the call (BUG).
    2. **Copy, then publish.** The files are copied into a temporary sibling and the dir is
       `rename()`d into place, so a half-copied dir is never visible and is never mistaken
       for a finished delivery.
    3. **A validated fd, never a path string.** ``shared/`` is writable by every run of the
       thread, so an earlier turn can plant ``attachments`` or ``<key>`` as a symlink — and
       can replace it AFTER a path-based check and before the copy. So every step below runs
       against an open directory fd: the inode checked is the inode written, chowned and
       renamed, and a swap between the check and the write is not reachable (SEC-5).
       ``attachments/`` itself is 2750 like ``downloads/``: only this call writes it, the run
       reads it through ``THREAD_GID``.
    """
    from .runner.uids import THREAD_GID
    parsed = parse_attachments(raw)
    if parsed is None:
        return
    key, files = parsed
    group = THREAD_GID if gid is None else gid
    shared.mkdir(parents=True, exist_ok=True)
    fd = os.open(shared, _OPEN_DIR)
    try:
        root = _thread_subdir(fd, ATTACHMENTS_DIRNAME, 0o2750, group)
    finally:
        os.close(fd)
    try:
        if _is_delivered(root, key, files):
            return
        staging = f".{key}.{os.getpid()}"
        _remove_at(root, staging)
        try:
            os.mkdir(staging, 0o700, dir_fd=root)
            staging_fd = _thread_subdir(root, staging, 0o700, -1)
            try:
                for source, name in files:
                    _copy_into(staging_fd, name, _source_path(source), group)
                os.fchown(staging_fd, -1, group)
                os.fchmod(staging_fd, 0o2750)   # after the group: a chown clears setgid
            finally:
                os.close(staging_fd)
            try:
                os.rename(staging, key, src_dir_fd=root, dst_dir_fd=root)
            except OSError:
                # Another preparation of the same job may have published first — but only a
                # COMPLETE dir is that. Anything else is a half-delivery or a planted entry,
                # and dropping our complete copy for it would run the turn short a file (BUG).
                if not _is_delivered(root, key, files):
                    raise
                _remove_at(root, staging)
        except BaseException:
            _remove_at(root, staging)
            raise
    finally:
        os.close(root)


def cleanup_artifacts_dir(artifacts_dir: Path) -> None:
    """Remove the artifacts directory. NOT called by the framework any more.

    Run dirs are retained for review; a run dir is wiped by
    ``prepare_artifacts_dir`` when the same run number is reused, and removed by
    ``artifacts:prune`` (``artifacts_retention.prune_run_dirs``) once its job ended long ago.
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
