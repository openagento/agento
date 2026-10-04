"""Panel re-login: the ``credential_login`` request row, shared by ``web`` and the cron worker.

``web`` inserts a ``pending`` row, shows the login URL, and seals a pasted code with the
row's public key. The worker (``credential:web-login``, cron, uid ``agent``) claims the row,
drives the vendor CLI through the authenticator's optional ``start_web_login``, and saves
the new credential with the same register-commit-dispatch path as ``credential:register``.

Every write the worker makes is fenced on the status it expects: a row that was cancelled,
swept or expired stops the worker at once. The pasted code is never stored in plain text:
the worker makes an RSA-3072 key pair for one login and keeps the private half only in its
memory; ``code_box`` and ``code_key`` are cleared when the code is read and on every
terminal write. Neither the code nor CLI output reaches a row or a log (SEC-6).
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
from pathlib import Path

import pymysql
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .auth import credentials_from_auth
from .credential_store import register_credential_and_dispatch
from .errors import CredentialLeasedError

ACTIVE = ("pending", "starting", "waiting", "verifying")
_ACTIVE_SQL = "('pending','starting','waiting','verifying')"
# A worker heartbeats every HEARTBEAT_S; a row silent for longer than STALE_S lost its worker.
HEARTBEAT_S = 1.0
STALE_S = 30
CLAIM_POLL_S = 2.0
WORKER_BUDGET_S = 55.0
CODE_MAX = 300  # RSA-3072 OAEP-SHA256 seals at most 318 bytes
TMP_ROOT = Path(tempfile.gettempdir()) / "agento-web-login"

_OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def seal_code(public_pem: str, code: str) -> bytes:
    key = serialization.load_pem_public_key(public_pem.encode())
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("code_key is not an RSA public key")
    return key.encrypt(code.encode(), _OAEP)


def open_code(private_key: rsa.RSAPrivateKey, box: bytes) -> str:
    return private_key.decrypt(box, _OAEP).decode()


# --- shared by web and the worker -------------------------------------------------------
#
# One lock order on every path: the credential row, then the login row. A write of a login
# row's ``status`` also takes a shared lock on its credential row (``status`` is in the
# foreign-key index), so a path that wrote the login row first and a path that locked the
# credential first (``_save``, ``request_login``) would deadlock. Every transaction that
# writes or locks a ``credential_login`` row therefore locks its credential row first.


def _lock_credential(cur, credential_id: int) -> bool:
    """Lock the credential row (the first lock of every login write). False: no such row."""
    cur.execute("SELECT id FROM credential WHERE id = %s FOR UPDATE", (credential_id,))
    return cur.fetchone() is not None


def _lock_for_login(cur, login_id: int) -> bool:
    """``_lock_credential`` for a login known by id. False: no such login.

    The plain read of ``credential_id`` is safe: it never changes. It also fixes the
    transaction's snapshot, so a later read of the login row must be a locking one."""
    cur.execute("SELECT credential_id FROM credential_login WHERE id = %s", (login_id,))
    row = cur.fetchone()
    return row is not None and _lock_credential(cur, row["credential_id"])


def mark_stale(conn: pymysql.Connection, credential_id: int | None = None) -> None:
    """Fail active rows past ``expires_at`` (``expired``) and running rows whose worker
    stopped its heartbeat (``abandoned``). Conditional on the active status; the caller
    commits. With ``credential_id`` the caller holds that credential's lock; without it,
    the credentials of all active logins are locked first (in id order), and only their
    rows are written."""
    with conn.cursor() as cur:
        if credential_id is None:
            cur.execute(f"SELECT DISTINCT credential_id FROM credential_login WHERE status IN {_ACTIVE_SQL}")
            ids = sorted(r["credential_id"] for r in cur.fetchall())
            if not ids:
                return
            marks = ",".join(["%s"] * len(ids))
            cur.execute(f"SELECT id FROM credential WHERE id IN ({marks}) ORDER BY id FOR UPDATE", ids)
            only, args = f" AND credential_id IN ({marks})", tuple(ids)
        else:
            only, args = " AND credential_id = %s", (credential_id,)
        cur.execute(
            "UPDATE credential_login SET status = 'failed', error_code = 'expired', code_box = NULL, "
            f"code_key = NULL WHERE status IN {_ACTIVE_SQL} AND expires_at <= UTC_TIMESTAMP(){only}",
            args,
        )
        cur.execute(
            "UPDATE credential_login SET status = 'failed', error_code = 'abandoned', code_box = NULL, "
            "code_key = NULL WHERE status IN ('starting','waiting','verifying') "
            f"AND heartbeat_at < UTC_TIMESTAMP() - INTERVAL {STALE_S} SECOND{only}",
            args,
        )


def request_login(
    conn: pymysql.Connection, credential_id: int, user_id: int | None, interactive_scopes: set[str],
) -> int | str:
    """Insert a ``pending`` login for the credential: its id, or why not (``not_found``,
    ``unsupported``, ``disabled``, ``not_oauth``, ``active``).

    Under the credential row lock, so two concurrent requests make one row. A stale active
    login is failed first, with the sweep's own fenced write."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT scope, type, enabled FROM credential WHERE id = %s FOR UPDATE", (credential_id,))
            cred = cur.fetchone()
            if cred is None:
                return "not_found"
            if cred["scope"] not in interactive_scopes:
                return "unsupported"
            if not cred["enabled"]:
                return "disabled"
            if cred["type"] != "oauth":
                return "not_oauth"
            mark_stale(conn, credential_id)
            cur.execute(
                f"SELECT 1 FROM credential_login WHERE credential_id = %s AND status IN {_ACTIVE_SQL} "
                "LIMIT 1 FOR UPDATE",
                (credential_id,),
            )
            if cur.fetchone():
                return "active"
            cur.execute(
                "INSERT INTO credential_login (credential_id, status, created_by, expires_at) "
                "VALUES (%s, 'pending', %s, UTC_TIMESTAMP() + INTERVAL 15 MINUTE)",
                (credential_id, user_id),
            )
            return int(cur.lastrowid)
    finally:
        conn.commit()


def get_login(conn: pymysql.Connection, login_id: int) -> dict | None:
    """The fields the panel shows. Never ``code_key`` or ``code_box``."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, verify_url, user_code, needs_code, error_code, expires_at "
            "FROM credential_login WHERE id = %s",
            (login_id,),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def put_code(conn: pymysql.Connection, login_id: int, code: str) -> str | None:
    """Seal ``code`` into the row: ``None`` when stored, else ``not_found`` or ``conflict``
    (not waiting for a code, or a code is already there: one code per login)."""
    try:
        with conn.cursor() as cur:
            if not _lock_for_login(cur, login_id):
                return "not_found"
            cur.execute(
                "SELECT status, needs_code, code_key, code_box IS NOT NULL AS has_box "
                "FROM credential_login WHERE id = %s FOR UPDATE",
                (login_id,),
            )
            row = cur.fetchone()
            if row is None:
                return "not_found"
            if row["status"] != "waiting" or not row["needs_code"] or not row["code_key"] or row["has_box"]:
                return "conflict"
            cur.execute(
                "UPDATE credential_login SET code_box = %s WHERE id = %s AND status = 'waiting' "
                "AND code_box IS NULL",
                (seal_code(row["code_key"], code), login_id),
            )
            return None
    finally:
        conn.commit()


def cancel_login(conn: pymysql.Connection, login_id: int) -> bool:
    """Cancel an active login (its fenced worker then stops). False when the row is unknown."""
    with conn.cursor() as cur:
        found = _lock_for_login(cur, login_id)
        if found:
            cur.execute(
                "UPDATE credential_login SET status = 'cancelled', code_box = NULL, code_key = NULL "
                f"WHERE id = %s AND status IN {_ACTIVE_SQL}",
                (login_id,),
            )
    conn.commit()
    return found


# --- the worker -------------------------------------------------------------------------


def sweep(conn: pymysql.Connection, tmp_root: Path = TMP_ROOT) -> None:
    """Fail stale rows, remove temp homes of logins that are no longer active (a killed
    worker runs no ``finally``), and delete rows finished more than a day ago.

    The homes are listed first and the active ids are read after, in a fresh transaction: a
    worker makes a home only after its claim commits, so a listed home of a live login is
    always in that read. A home made later is not listed."""
    homes = list(tmp_root.iterdir()) if tmp_root.is_dir() else []
    mark_stale(conn)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM credential_login WHERE status IN ('done','failed','cancelled') "
            "AND updated_at < UTC_TIMESTAMP() - INTERVAL 1 DAY"
        )
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(f"SELECT id FROM credential_login WHERE status IN {_ACTIVE_SQL}")
        active = {str(r["id"]) for r in cur.fetchall()}
    conn.commit()
    for home in homes:
        if home.name.split("-", 1)[0] not in active:
            shutil.rmtree(home, ignore_errors=True)


def claim(conn: pymysql.Connection, login_id: int) -> bool:
    """Take a ``pending`` row; the heartbeat starts with the claim. True for one worker only."""
    return _fenced(conn, login_id, ("pending",), "status = 'starting', heartbeat_at = UTC_TIMESTAMP()")


def run_worker(
    conn: pymysql.Connection, logger: logging.Logger, tmp_root: Path = TMP_ROOT,
    budget_s: float = WORKER_BUDGET_S,
) -> None:
    """One cron tick: sweep, then claim and run pending logins while ``budget_s`` lasts. A
    claimed login runs to its end, even past the budget; no claim is made after it."""
    deadline = time.monotonic() + budget_s
    sweep(conn, tmp_root)
    while True:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM credential_login WHERE status = 'pending' ORDER BY id")
            pending = [r["id"] for r in cur.fetchall()]
        conn.commit()
        for login_id in pending:
            if time.monotonic() >= deadline:
                return
            if claim(conn, login_id):
                run_login(conn, login_id, logger, tmp_root)
        if time.monotonic() >= deadline:
            return
        time.sleep(CLAIM_POLL_S)


def run_login(conn: pymysql.Connection, login_id: int, logger: logging.Logger, tmp_root: Path = TMP_ROOT) -> None:
    """Drive one claimed (``starting``) login to a terminal status."""
    from ..harness import get_authenticator

    with conn.cursor() as cur:
        cur.execute(
            "SELECT l.credential_id, c.scope FROM credential_login l "
            "JOIN credential c ON c.id = l.credential_id WHERE l.id = %s",
            (login_id,),
        )
        row = cur.fetchone()
    conn.commit()
    # `start_web_login` is an optional member of CredentialAuthenticator (protocols.py):
    # an authenticator without it cannot re-login from the panel.
    start = getattr(get_authenticator(row["scope"]), "start_web_login", None) if row else None
    if start is None:
        _finish(conn, login_id, "unsupported", logger)
        return
    tmp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix=f"{login_id}-", dir=tmp_root))  # 0700
    login = None
    try:
        try:
            login = start(str(home), logger)
        except Exception as exc:
            logger.warning("credential login %s: the CLI did not start (%s)", login_id, type(exc).__name__)
            _finish(conn, login_id, "cli_failed", logger)
            return
        prompt = login.prompt
        if not prompt.url.startswith("https://"):
            _finish(conn, login_id, "bad_url", logger)
            return
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072) if prompt.needs_code else None
        public_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode() if key else None
        if not _fenced(
            conn, login_id, ("starting",),
            "status = 'waiting', verify_url = %s, user_code = %s, needs_code = %s, code_key = %s, "
            "heartbeat_at = UTC_TIMESTAMP()",
            (prompt.url, prompt.user_code, prompt.needs_code, public_pem),
        ):
            return
        _watch(conn, login_id, row["credential_id"], login, key, logger)
    finally:
        if login is not None:
            login.close()
        shutil.rmtree(home, ignore_errors=True)


def _heartbeat(conn, login_id: int) -> dict | None:
    """Beat, and read the row back: ``None`` when it is gone."""
    with conn.cursor() as cur:
        _lock_for_login(cur, login_id)
        cur.execute(
            "UPDATE credential_login SET heartbeat_at = UTC_TIMESTAMP() "
            "WHERE id = %s AND status IN ('waiting','verifying')",
            (login_id,),
        )
        # Read back instead of trusting rowcount: a heartbeat in the same second
        # changes no row.
        cur.execute(
            "SELECT status, code_box IS NOT NULL AS has_box, expires_at <= UTC_TIMESTAMP() AS expired "
            "FROM credential_login WHERE id = %s FOR UPDATE",
            (login_id,),
        )
        state = cur.fetchone()
    conn.commit()
    return state


def _watch(conn, login_id: int, credential_id: int, login, key, logger: logging.Logger) -> None:
    while True:
        time.sleep(HEARTBEAT_S)
        state = _heartbeat(conn, login_id)
        if state is None or state["status"] not in ("waiting", "verifying"):
            return  # cancelled or swept
        if state["expired"]:
            _finish(conn, login_id, "expired", logger)
            return
        if key is not None and state["has_box"] and state["status"] == "waiting":
            code = _take_code(conn, login_id, key, logger)
            if code is None:
                return
            login.submit_code(code)
        try:
            result = login.poll()
        except Exception as exc:
            logger.warning("credential login %s: the CLI login failed (%s)", login_id, type(exc).__name__)
            _finish(conn, login_id, "cli_failed", logger)
            return
        if result is not None:
            _save(conn, login_id, credential_id, result, needs_code=key is not None, logger=logger)
            return


def _take_code(conn, login_id: int, key: rsa.RSAPrivateKey, logger: logging.Logger) -> str | None:
    """Consume the sealed code once: clear it and move to ``verifying`` in one transaction.
    ``None``: the row moved on, or the box does not open with this worker's key."""
    with conn.cursor() as cur:
        _lock_for_login(cur, login_id)
        cur.execute(
            "SELECT code_box FROM credential_login WHERE id = %s AND status = 'waiting' FOR UPDATE",
            (login_id,),
        )
        row = cur.fetchone()
        if row is None or row["code_box"] is None:
            conn.rollback()
            return None
        cur.execute(
            "UPDATE credential_login SET code_box = NULL, code_key = NULL, status = 'verifying' "
            "WHERE id = %s AND status = 'waiting'",
            (login_id,),
        )
    conn.commit()
    try:
        return open_code(key, bytes(row["code_box"]))
    except ValueError:
        _finish(conn, login_id, "cli_failed", logger)
        return None


def _save(conn, login_id: int, credential_id: int, result, *, needs_code: bool, logger: logging.Logger) -> None:
    if not needs_code and not _fenced(conn, login_id, ("waiting",), "status = 'verifying'"):
        return
    credentials = credentials_from_auth(result)
    with conn.cursor() as cur:
        # The global lock order (top of the file): credential row, then login row.
        cur.execute(
            "SELECT scope, label, enabled, token_limit FROM credential WHERE id = %s FOR UPDATE",
            (credential_id,),
        )
        cred = cur.fetchone()
        # Expiry with DB time, under the lock: the credential lock above can wait past it.
        cur.execute(
            "SELECT status, expires_at <= UTC_TIMESTAMP() AS expired FROM credential_login "
            "WHERE id = %s FOR UPDATE",
            (login_id,),
        )
        login_row = cur.fetchone()
        if login_row is None or login_row["status"] != "verifying" or cred is None:
            conn.rollback()
            if login_row is not None and login_row["status"] == "waiting":
                _finish(conn, login_id, "cli_failed", logger)  # the CLI ended before a code came
            return  # else a cancel or a sweep won
        if login_row["expired"]:
            conn.rollback()
            _finish(conn, login_id, "expired", logger)
            return
        if not cred["enabled"]:
            # An operator disable mid-login wins: registering would enable it again (SEC-7).
            conn.rollback()
            _finish(conn, login_id, "disabled", logger)
            return
        cur.execute(
            "UPDATE credential_login SET status = 'done', error_code = NULL, code_box = NULL, "
            "code_key = NULL WHERE id = %s AND status = 'verifying'",
            (login_id,),
        )
    try:
        register_credential_and_dispatch(
            conn, scope=cred["scope"], label=cred["label"], credentials=credentials,
            token_limit=cred["token_limit"], type="oauth", logger=logger,
        )
    except CredentialLeasedError:
        _finish(conn, login_id, "busy", logger)
        return
    logger.info("credential login %s: credential id=%s saved", login_id, credential_id)


def _fenced(conn, login_id: int, expected: tuple[str, ...], set_sql: str, args: tuple = ()) -> bool:
    """``UPDATE … WHERE id AND status IN expected``, committed. False: the row moved on.

    Every caller changes ``status``, so a matching row always counts as changed."""
    marks = ",".join(["%s"] * len(expected))
    with conn.cursor() as cur:
        _lock_for_login(cur, login_id)
        cur.execute(
            f"UPDATE credential_login SET {set_sql} WHERE id = %s AND status IN ({marks})",
            (*args, login_id, *expected),
        )
        changed = cur.rowcount == 1
    conn.commit()
    return changed


def _finish(conn, login_id: int, error_code: str, logger: logging.Logger) -> None:
    """The terminal ``failed`` write: fenced on an active status, clears the box and key."""
    _fenced(conn, login_id, ACTIVE,
            "status = 'failed', error_code = %s, code_box = NULL, code_key = NULL", (error_code,))
    logger.warning("credential login %s: failed (%s)", login_id, error_code)
