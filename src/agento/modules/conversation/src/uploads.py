"""Files a panel user hands the agent, before a run copies them into its thread.

The staging tree is ``<workspace>/uploads/t<conversation_id>/u<user_id>/<upload_id>/<name>``.
Its two leading components are the **ownership record**: both are derived by the server (the
conversation the route already authorized, the session's user), so an ``upload_id`` from
another thread or another user is simply not there. No table, no second source of truth, and
no caller string ever selects a directory (SEC-7).

A staged file has exactly three operations - **created** here, **read** by run preparation,
**removed** by the sweep. Nothing renames or moves it, which is what makes a failure anywhere
in the send path harmless: the worst outcome is a dir nobody attached, which the sweep takes.

Creation and removal are ordered by one per-conversation named lock, ``cv_up:<id>``, so the
sweep can never decide "unreferenced" against a state a submission is about to change.
"""
from __future__ import annotations

import contextlib
import email.parser
import email.policy
import os
import re
import secrets
import shutil
import time
import unicodedata
from pathlib import Path

from agento.framework.workspace_paths import UPLOADS_DIR

UPLOAD_ID = re.compile(r"[0-9a-f]{32}")
LOCK_WAIT_SECONDS = 10
MAX_NAME_BYTES = 120
_FALLBACK_NAME = "file"


class UploadError(RuntimeError):
    """A staging operation the caller must answer with `status`. It names no path (SEC-6)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def user_dir(conversation_id: int, user_id: int) -> Path:
    """Where this user's uploads for this conversation live. Server-derived, both parts."""
    return Path(UPLOADS_DIR) / f"t{int(conversation_id)}" / f"u{int(user_id)}"


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
    """`name`, or `name-2`, `name-3`… when the message already has that name.

    The whole message is one directory, so two uploads both called `report.pdf` must not
    collide: the second becomes `report-2.pdf`. Bounded by `uploads_per_message`.
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


@contextlib.contextmanager
def uploads_lock(conn, conversation_id: int):
    """Hold the conversation's upload lock: creation and removal never interleave.

    The same `GET_LOCK` shape as the versioned-artifacts retention lock
    (`framework/access/launches.py`). A named lock belongs to its CONNECTION and survives
    COMMIT, so this must never run on a `db.pooled` one (`framework/db.py`).
    """
    name = f"cv_up:{int(conversation_id)}"
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, %s) AS got", (name, LOCK_WAIT_SECONDS))
        row = cur.fetchone()
    if not row or row["got"] != 1:
        raise UploadError(503, "uploads are busy, try again")
    try:
        yield
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT RELEASE_LOCK(%s)", (name,))


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


def store(conversation_id: int, user_id: int, name: str, data: bytes) -> tuple[str, str]:
    """Write one staged file. Returns `(upload_id, stored name)`.

    The dirs are 0700 and the file 0600, under a `<workspace>/uploads/` that is itself
    `agent`-owned 0700: a run has a per-run uid, never `agent`, so no run can read any of it.
    """
    upload_id = secrets.token_hex(16)
    stored = safe_name(name)
    root = Path(UPLOADS_DIR)
    root.mkdir(mode=0o700, exist_ok=True)
    # The root gets the same check as every component below it: it lives in the workspace, and
    # a link planted there would steer every staged file somewhere else (SEC-5).
    if root.is_symlink() or not root.is_dir():
        raise UploadError(500, "upload failed")
    parent = _open_dir(root, f"t{int(conversation_id)}", 0o700)
    parent = _open_dir(parent, f"u{int(user_id)}", 0o700)
    target = _open_dir(parent, upload_id, 0o700)
    path = target / stored
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return upload_id, stored


def descriptor(conversation_id: int, user_id: int, upload_ids: list[str], key: str) -> dict:
    """The `{key, files:[{from, as}]}` descriptor for a message, or raise.

    `from` is relative to the workspace root, so the framework can resolve it without
    knowing anything about conversations (MOD-1). An id whose dir is not there - another
    thread's, another user's, or one the sweep already took - refuses the submission.
    """
    base = user_dir(conversation_id, user_id)
    files: list[dict] = []
    taken: set[str] = set()
    for upload_id in upload_ids:
        if not UPLOAD_ID.fullmatch(upload_id):
            raise UploadError(400, "upload not available")
        try:
            entries = [e for e in os.listdir(base / upload_id) if not e.startswith(".")]
        except OSError as exc:
            raise UploadError(400, "upload not available") from exc
        if len(entries) != 1:
            raise UploadError(400, "upload not available")
        stored = entries[0]
        name = unique_name(safe_name(stored), taken)
        taken.add(name)
        files.append({"from": str((base / upload_id / stored).relative_to(Path(UPLOADS_DIR).parent)),
                      "as": name})
    return {"key": key, "files": files}


def prompt_block(uploads: dict) -> str:
    """The lines the agent reads. The paths are the run's own, never the staging ones."""
    key = uploads["key"]
    lines = "\n".join(f"- ./shared/attachments/{key}/{f['as']}" for f in uploads["files"])
    return f"\n\nAttached files:\n{lines}"


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


def expired_dirs(conversation_id: int, user_id: int, older_than_hours: int) -> list[str]:
    """The upload ids of that user's dirs older than the retention. Caller holds the lock."""
    base = user_dir(conversation_id, user_id)
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


def remove(conversation_id: int, user_id: int, upload_id: str) -> None:
    """Take one staged dir. Caller holds the conversation's lock."""
    if not UPLOAD_ID.fullmatch(upload_id):
        return
    shutil.rmtree(user_dir(conversation_id, user_id) / upload_id, ignore_errors=True)


def users_with_uploads(conversation_id: int) -> list[int]:
    """The user ids that have a staging dir in this conversation."""
    try:
        names = os.listdir(Path(UPLOADS_DIR) / f"t{int(conversation_id)}")
    except OSError:
        return []
    return [int(n[1:]) for n in names if re.fullmatch(r"u[0-9]{1,19}", n)]


def conversations_with_uploads() -> list[int]:
    """The conversation ids that have a staging tree."""
    try:
        names = os.listdir(UPLOADS_DIR)
    except OSError:
        return []
    return [int(n[1:]) for n in names if re.fullmatch(r"t[0-9]{1,19}", n)]
