"""Files a panel user hands the agent, before a run copies them into its thread.

The staging tree is ``<workspace>/uploads/<owner>/u<user_id>/<upload_id>/<name>``. ``<owner>``
is one validated component that says what the files belong to: ``t<conversation_id>`` for a
message, ``s<schedule_id>`` for a schedule (``s0``: a schedule that does not exist yet). The
two leading components are the **ownership record**: both are derived by the server (the row
the route already authorized, the session's user), so an ``upload_id`` from another owner or
another user is simply not there. No table, no second source of truth, and no caller string
ever selects a directory (SEC-7).

A staged file has exactly three operations - **created** here, **read** by run preparation,
**removed** by its owner's sweep. Nothing renames or moves it, which is what makes a failure
anywhere in a claim harmless: the worst outcome is a dir nobody named, which the sweep takes.
Each owning module orders its claims against its sweep with its own lock.
"""
from __future__ import annotations

import contextlib
import email.parser
import email.policy
import json
import os
import re
import secrets
import shutil
import time
import unicodedata
from pathlib import Path

from .workspace_paths import UPLOADS_DIR

UPLOAD_ID = re.compile(r"[0-9a-f]{32}")
OWNER = re.compile(r"[ts][0-9]{1,19}")
MAX_NAME_BYTES = 120
_FALLBACK_NAME = "file"


class UploadError(RuntimeError):
    """A staging operation the caller must answer with `status`. It names no path (SEC-6)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def user_dir(owner: str, user_id: int) -> Path:
    """Where this user's uploads for this owner live. Server-derived, both parts.

    The ONE place an owner becomes a path: every function below builds its path here, so an
    owner that is not `t<digits>` or `s<digits>` reaches no directory (SEC-5)."""
    if not isinstance(owner, str) or not OWNER.fullmatch(owner):
        raise UploadError(500, "upload failed")
    return Path(UPLOADS_DIR) / owner / f"u{int(user_id)}"


def safe_name(raw: object) -> str:
    """A client file name as ONE path component: basename, NFC, no dot-file, bounded.

    Never trusted as a path (SEC-5): the separators go before anything else looks at it, and
    a name that sanitizes to nothing becomes `file`.
    """
    text = raw if isinstance(raw, str) else ""
    text = unicodedata.normalize("NFC", text).replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(ch for ch in text if ch.isprintable() and ch not in '/\0').strip()
    text = text.lstrip(".").strip()
    while len(text.encode("utf-8")) > MAX_NAME_BYTES:
        text = text[:-1]
    return text or _FALLBACK_NAME


def unique_name(name: str, taken: set[str]) -> str:
    """`name`, or `name-2`, `name-3`… when the file set already has that name.

    One set lands in one directory, so two uploads both called `report.pdf` must not
    collide: the second becomes `report-2.pdf`. Bounded by the caller's cap on a set.
    """
    if name not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for n in range(2, 1000):
        candidate = f"{stem}-{n}{'.' + ext if ext else ''}"
        if candidate not in taken:
            return candidate
    raise UploadError(400, "too many files with that name")


def _open_dir(parent: Path, name: str, mode: int) -> Path:
    """Make `name` under `parent` and refuse an existing entry that is not our own dir.

    `os.mkdir` + `O_NOFOLLOW` on the result: a planted symlink cannot redirect the write
    (SEC-5), the same discipline `framework/artifacts_dir._thread_subdir` applies to the
    thread tree.
    """
    path = parent / name
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, mode)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise UploadError(500, "upload failed") from exc
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise UploadError(500, "upload failed")
    finally:
        os.close(fd)
    return path


def store(owner: str, user_id: int, name: str, data: bytes) -> tuple[str, str]:
    """Write one staged file. Returns `(upload_id, stored name)`.

    The dirs are 0700 and the file 0600, under a `<workspace>/uploads/` that is itself
    `agent`-owned 0700: a run has a per-run uid, never `agent`, so no run can read any of it.
    """
    base = user_dir(owner, user_id)          # validates the owner before anything is made
    upload_id = secrets.token_hex(16)
    stored = safe_name(name)
    root = Path(UPLOADS_DIR)
    root.mkdir(mode=0o700, exist_ok=True)
    # The root gets the same check as every component below it: it lives in the workspace, and
    # a link planted there would steer every staged file somewhere else (SEC-5).
    if root.is_symlink() or not root.is_dir():
        raise UploadError(500, "upload failed")
    parent = _open_dir(root, base.parent.name, 0o700)
    parent = _open_dir(parent, base.name, 0o700)
    target = _open_dir(parent, upload_id, 0o700)
    path = target / stored
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return upload_id, stored


def _staged_file(owner: str, user_id: int, upload_id: str, *, read: bool = False) -> tuple[str, bytes]:
    """The one stored name of a staged upload, and its bytes when asked.

    Every component from the root down opens with `O_NOFOLLOW` relative to its parent's fd, so
    a link planted at the owner, user, upload or file level is refused, not followed (SEC-5).
    A dir that is not there - another owner's, another user's, one the sweep took - refuses too.
    """
    base = user_dir(owner, user_id)
    if not UPLOAD_ID.fullmatch(upload_id):
        raise UploadError(400, "upload not available")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(UPLOADS_DIR, flags)
        for name in (base.parent.name, base.name, upload_id):
            below = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = below
        entries = [e for e in os.listdir(fd) if not e.startswith(".")]
        if len(entries) != 1:
            raise UploadError(400, "upload not available")
        data = b""
        if read:
            with os.fdopen(os.open(entries[0], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd), "rb") as fh:
                data = fh.read()
        return entries[0], data
    except OSError as exc:
        raise UploadError(400, "upload not available") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def descriptor(owner: str, user_id: int, upload_ids: list[str], key: str, *,
               taken: set[str] | None = None) -> dict:
    """The `{key, files:[{from, as}]}` descriptor for these staged uploads, or raise.

    `from` is relative to the workspace root, so run preparation resolves it without knowing
    what an owner means. An id that is not this owner's and user's staged upload refuses the
    claim. `taken` holds the names the caller's set already uses, and gets each name given here.
    """
    base = user_dir(owner, user_id)
    files: list[dict] = []
    taken = set() if taken is None else taken
    for upload_id in upload_ids:
        stored, _ = _staged_file(owner, user_id, upload_id)
        name = unique_name(safe_name(stored), taken)
        taken.add(name)
        files.append({"from": str((base / upload_id / stored).relative_to(Path(UPLOADS_DIR).parent)),
                      "as": name})
    return {"key": key, "files": files}


def copy(source: str, target: str, user_id: int, upload_id: str) -> str:
    """Stage under `target` a copy of that user's upload under `source`. Returns the new id.
    The source stays: its owner's sweep takes it."""
    stored, data = _staged_file(source, user_id, upload_id, read=True)
    return store(target, user_id, stored, data)[0]


def staged(text: str | None) -> list[dict]:
    """What a descriptor names, for a reader of its owner: `[{upload_id, name, bytes}]`.
    `bytes` is None when the staged file is gone. A `from` path never leaves here (SEC-6)."""
    out = []
    for entry in (json.loads(text)["files"] if text else []):
        source = Path(entry["from"])
        try:
            size = os.stat(Path(UPLOADS_DIR).parent / source, follow_symlinks=False).st_size
        except OSError:
            size = None
        out.append({"upload_id": source.parent.name, "name": entry["as"], "bytes": size})
    return out


def descriptor_json(value: object) -> str | None:
    """`message.uploads` as the JSON text the framework takes. PyMySQL hands a JSON column
    back as text, bytes or an already decoded object depending on the server."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value if isinstance(value, str) else json.dumps(value)


def parse_one_file(body: bytes, content_type: str) -> tuple[str, bytes]:
    """The single file part of a `multipart/form-data` body: `(filename, bytes)`.

    stdlib only (SEC-11): the body is re-headed with its own Content-Type and handed to the
    email parser, which is the multipart parser Python ships. Exactly one file part is
    accepted - a body with none, or with several, is a bad request.
    """
    if "multipart/form-data" not in content_type.lower():
        raise UploadError(400, "Content-Type must be multipart/form-data")
    try:
        message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1", "replace") + b"\r\n\r\n" + body)
        parts = [p for p in message.iter_parts() if p.get_filename() is not None]
    except Exception as exc:                        # a malformed body is the caller's fault
        raise UploadError(400, "upload failed") from exc
    if len(parts) != 1:
        raise UploadError(400, "send exactly one file")
    payload = parts[0].get_payload(decode=True)
    if not isinstance(payload, bytes) or not payload:
        raise UploadError(400, "the file is empty")
    return safe_name(parts[0].get_filename()), payload


def expired_dirs(owner: str, user_id: int, older_than_hours: int) -> list[str]:
    """The upload ids of that user's dirs older than the retention. Caller holds the lock."""
    base = user_dir(owner, user_id)
    cutoff = time.time() - older_than_hours * 3600
    try:
        names = os.listdir(base)
    except OSError:
        return []
    out = []
    for name in names:
        if not UPLOAD_ID.fullmatch(name):
            continue
        try:
            if (base / name).stat().st_mtime < cutoff:
                out.append(name)
        except OSError:
            continue
    return out


def remove(owner: str, user_id: int, upload_id: str) -> None:
    """Take one staged dir. Caller holds the owner's lock."""
    if not UPLOAD_ID.fullmatch(upload_id):
        return
    shutil.rmtree(user_dir(owner, user_id) / upload_id, ignore_errors=True)


def prune(owner: str) -> None:
    """Take the owner's EMPTY dirs. Only for an owner no route can stage under any more."""
    base = user_dir(owner, 0).parent
    for user_id in users(owner):
        with contextlib.suppress(OSError):
            os.rmdir(user_dir(owner, user_id))
    with contextlib.suppress(OSError):
        os.rmdir(base)


def users(owner: str) -> list[int]:
    """The user ids that have a staging dir under this owner."""
    try:
        names = os.listdir(user_dir(owner, 0).parent)
    except OSError:
        return []
    return [int(n[1:]) for n in names if re.fullmatch(r"u[0-9]{1,19}", n)]


def owners(prefix: str) -> list[int]:
    """The ids of the `<prefix><digits>` staging trees: `owners("t")` are conversation ids."""
    try:
        names = os.listdir(UPLOADS_DIR)
    except OSError:
        return []
    return [int(n[1:]) for n in names if n[:1] == prefix and OWNER.fullmatch(n)]


def max_bytes(conn) -> int:
    """``core/uploads/max_bytes``: the size cap of one staged file, for every owner."""
    from .bootstrap import CORE_MODULES_DIR
    from .config_resolver import load_db_overrides, read_config_defaults, resolve_field

    core = Path(CORE_MODULES_DIR) / "core"
    schema = json.loads((core / "system.json").read_text())["uploads/max_bytes"]
    return int(resolve_field("core", "uploads/max_bytes", schema, read_config_defaults(core),
                             load_db_overrides(conn)).value)


def check_bytes(conn, data: bytes) -> None:
    """The refusal NAMES the limit: a user told only "too large" has to guess, or read docs
    they do not have. The number is the operator's own config value."""
    cap = max_bytes(conn)
    if len(data) > cap:
        raise UploadError(413, f"file too large (max {cap // (1024 * 1024)} MB)"
                          if cap >= 1024 * 1024 else f"file too large (max {cap // 1024} KB)")
