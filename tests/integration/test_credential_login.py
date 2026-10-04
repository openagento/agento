"""Panel re-login against real MySQL: the web routes, and the cron worker driving fake vendor
CLIs (honest doubles: they print what the real CLIs print, read a line, write the files the
real CLIs write, and exit 0 or 1)."""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import textwrap
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agento.framework.access import accounts, sessions
from agento.framework.agent_manager import credential_login
from agento.framework.agent_manager.models import CredentialRecord, encrypt_credentials
from agento.modules.claude.src.auth import ClaudeCredentialAuthenticator
from agento.modules.codex.src.auth import CodexCredentialAuthenticator
from agento.web import admin_api, api

from .conftest import _test_connection

_admin: dict[str, int] = {}  # the id of the `cl-admin` user row, set by `db`
CODE = "pasted-code#st4te"

FAKE_CLAUDE = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys
    print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&state=st4te", flush=True)
    code = input("Paste code here if prompted > ")
    if code != {CODE!r}:
        sys.exit(1)
    home = os.environ["HOME"]
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    json.dump({{"claudeAiOauth": {{"accessToken": "new-at", "refreshToken": "new-rt"}}}},
              open(os.path.join(home, ".claude", ".credentials.json"), "w"))
""")

# Device flow. It waits for `$HOME/go` so a test can act while the login runs.
FAKE_CODEX = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys, time
    home = os.environ["HOME"]
    open(os.path.join(home, "cli.pid"), "w").write(str(os.getpid()))
    print("1. Open this link in your browser and sign in to your account")
    print("   https://auth.openai.com/codex/device")
    print("2. Enter this one-time code (expires in 15 minutes)")
    print("   WXYZ-12345", flush=True)
    while not os.path.exists(os.path.join(home, "go")):
        time.sleep(0.02)
    os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
    json.dump({{"tokens": {{"access_token": "new-at", "refresh_token": "new-rt", "account_id": "acc"}}}},
              open(os.path.join(home, ".codex", "auth.json"), "w"))
""")


@pytest.fixture
def db():
    c = _test_connection(autocommit=True)
    _clean(c)
    with c.cursor() as cur:
        cur.execute("INSERT INTO `user` (username, role) VALUES ('cl-admin', 'admin')")
        _admin["id"] = cur.lastrowid
    yield c
    _clean(c)
    c.close()


def _clean(c):
    with c.cursor() as cur:
        cur.execute("DELETE FROM credential WHERE label LIKE 'cl-%%'")  # logins cascade
        cur.execute("DELETE FROM `user` WHERE username = 'cl-admin'")


@pytest.fixture
def clis(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(credential_login, "HEARTBEAT_S", 0.05)
    authenticators = {"claude": ClaudeCredentialAuthenticator(), "codex": CodexCredentialAuthenticator()}
    monkeypatch.setattr("agento.framework.harness.get_authenticator", authenticators.get)
    return tmp_path / "logins"


@pytest.fixture
def events():
    manager = MagicMock()
    with patch("agento.framework.event_manager.get_event_manager", return_value=manager):
        yield manager


def _credential(conn, scope: str, *, enabled=True, type_="oauth", label="cl-main") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO credential (agent_type, scope, type, label, credentials, enabled) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (scope, scope, type_, label, encrypt_credentials({"subscription_key": "old-at"}), enabled),
        )
        return cur.lastrowid


def _req(conn, method="GET", body=None, **params) -> api.Request:
    admin = accounts.User(id=_admin["id"], username="cl-admin", role="admin", is_active=True)
    session = sessions.Session(id="sid", user=admin,
                               expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))
    return api.Request(method=method, path="/", headers={}, body=b"", cookies={}, origins=None,
                       params={k: str(v) for k, v in params.items()}, conn=conn, session=session, json=body)


def _web(handler, method="GET", body=None, **params) -> api.Response:
    conn = _test_connection()
    try:
        return handler(_req(conn, method, body, **params))
    finally:
        conn.close()


def _row(conn, login_id) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM credential_login WHERE id = %s", (login_id,))
        return cur.fetchone()


def _stored(conn, credential_id) -> CredentialRecord:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM credential WHERE id = %s", (credential_id,))
        return CredentialRecord.from_row(cur.fetchone())


def _wait(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def _start_worker(login_id, tmp_root, logger) -> threading.Thread:
    def run():
        conn = _test_connection()
        try:
            assert credential_login.claim(conn, login_id)
            credential_login.run_login(conn, login_id, logger, tmp_root)
        finally:
            conn.close()

    t = threading.Thread(target=run)
    t.start()
    return t


def _login(conn, credential_id) -> int:
    resp = _web(admin_api.start_credential_login, "POST", id=credential_id)
    assert resp.status == 201, resp.body
    return resp.body["id"]


def _home(tmp_root: Path) -> Path:
    _wait(lambda: tmp_root.is_dir() and any(tmp_root.iterdir()))
    return next(tmp_root.iterdir())


def _assert_terminal(row, status, error_code=None):
    assert (row["status"], row["error_code"]) == (status, error_code)
    assert row["code_box"] is None and row["code_key"] is None


def test_claude_code_flow_end_to_end(db, clis, events, caplog):
    cid = _credential(db, "claude")
    login_id = _login(db, cid)
    state = _web(admin_api.credential_login_state, id=login_id).body
    assert state["status"] == "pending" and set(state) == {
        "status", "verify_url", "user_code", "needs_code", "error_code", "expires_at"}

    caplog.set_level(logging.DEBUG)
    worker = _start_worker(login_id, clis, logging.getLogger("cl-test"))
    _wait(lambda: _row(db, login_id)["status"] == "waiting")
    state = _web(admin_api.credential_login_state, id=login_id).body
    assert state["verify_url"] == "https://claude.com/cai/oauth/authorize?code=true&state=st4te"
    assert state["needs_code"] is True and "code_key" not in state and "code_box" not in state
    assert _row(db, login_id)["code_key"].startswith("-----BEGIN PUBLIC KEY-----")

    assert _web(admin_api.credential_login_code, "POST", {"code": CODE}, id=login_id).status == 204
    assert CODE.encode() not in bytes(_row(db, login_id)["code_box"] or b"")
    worker.join(20)
    assert not worker.is_alive()

    _assert_terminal(_row(db, login_id), "done")
    stored = _stored(db, cid)
    assert stored.credentials["subscription_key"] == "new-at" and stored.enabled
    (_name, event), = [c.args for c in events.dispatch.call_args_list if c.args[0] == "credential_register_after"]
    assert (event.credential_id, event.scope, event.label, event.type) == (cid, "claude", "cl-main", "oauth")
    assert event.credentials["refresh_token"] == "new-rt"
    assert not any(clis.iterdir())  # temp HOME removed
    assert CODE not in caplog.text and "new-at" not in caplog.text


def test_codex_device_flow_end_to_end(db, clis, events):
    cid = _credential(db, "codex")
    login_id = _login(db, cid)
    worker = _start_worker(login_id, clis, logging.getLogger("cl-test"))
    _wait(lambda: _row(db, login_id)["status"] == "waiting")
    state = _web(admin_api.credential_login_state, id=login_id).body
    assert (state["verify_url"], state["user_code"], state["needs_code"]) == (
        "https://auth.openai.com/codex/device", "WXYZ-12345", False)
    assert _row(db, login_id)["code_key"] is None
    # A device flow takes no code.
    assert _web(admin_api.credential_login_code, "POST", {"code": "x"}, id=login_id).status == 409

    (_home(clis) / "go").touch()
    worker.join(20)
    _assert_terminal(_row(db, login_id), "done")
    assert _stored(db, cid).credentials["raw_auth"]["tokens"]["account_id"] == "acc"
    assert [c.args[0] for c in events.dispatch.call_args_list].count("credential_register_after") == 1
    assert not any(clis.iterdir())


def test_request_refusals(db, monkeypatch):
    assert _web(admin_api.start_credential_login, "POST", id=999999999).status == 404
    disabled = _credential(db, "claude", enabled=False, label="cl-off")
    assert _web(admin_api.start_credential_login, "POST", id=disabled).status == 409
    api_key = _credential(db, "claude", type_="anthropic_api_key", label="cl-key")
    assert _web(admin_api.start_credential_login, "POST", id=api_key).status == 409
    no_mode = _credential(db, "openrouter", label="cl-or")
    assert _web(admin_api.start_credential_login, "POST", id=no_mode).status == 400

    cid = _credential(db, "claude")
    first = _login(db, cid)
    assert _web(admin_api.start_credential_login, "POST", id=cid).status == 409
    # A stale login (its worker stopped the heartbeat) is failed, and a new one may start.
    with db.cursor() as cur:
        cur.execute("UPDATE credential_login SET status = 'waiting', heartbeat_at = UTC_TIMESTAMP() - "
                    "INTERVAL 31 SECOND WHERE id = %s", (first,))
    second = _login(db, cid)
    _assert_terminal(_row(db, first), "failed", "abandoned")
    assert _row(db, second)["status"] == "pending"


def test_two_concurrent_requests_make_one_row(db):
    cid = _credential(db, "claude")
    barrier = threading.Barrier(2)
    answers: list[int] = []

    def post():
        conn = _test_connection()
        try:
            barrier.wait()
            answers.append(admin_api.start_credential_login(_req(conn, "POST", id=cid)).status)
        finally:
            conn.close()

    threads = [threading.Thread(target=post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(answers) == [201, 409]
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM credential_login WHERE credential_id = %s", (cid,))
        assert cur.fetchone()["n"] == 1


def test_claim_race_one_winner(db):
    login_id = _login(db, _credential(db, "claude"))
    barrier = threading.Barrier(4)
    wins: list[bool] = []

    def claim():
        conn = _test_connection()
        try:
            barrier.wait()
            wins.append(credential_login.claim(conn, login_id))
        finally:
            conn.close()

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(wins) == [False, False, False, True]


def test_one_code_per_login_and_the_box_opens_only_with_the_worker_key(db):
    login_id = _login(db, _credential(db, "claude"))
    worker_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    from cryptography.hazmat.primitives import serialization
    pem = worker_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    with db.cursor() as cur:
        cur.execute("UPDATE credential_login SET status = 'waiting', needs_code = TRUE, code_key = %s, "
                    "heartbeat_at = UTC_TIMESTAMP() WHERE id = %s", (pem, login_id))
    longest = "~" * credential_login.CODE_MAX  # the longest code the route takes, sealed for real
    assert _web(admin_api.credential_login_code, "POST", {"code": longest}, id=login_id).status == 204
    assert _web(admin_api.credential_login_code, "POST", {"code": "replay"}, id=login_id).status == 409
    box = bytes(_row(db, login_id)["code_box"])
    assert credential_login.open_code(worker_key, box) == longest
    other = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    with pytest.raises(ValueError):
        credential_login.open_code(other, box)


def _codex_waiting(db, clis, *, label="cl-main"):
    cid = _credential(db, "codex", label=label)
    login_id = _login(db, cid)
    worker = _start_worker(login_id, clis, logging.getLogger("cl-test"))
    _wait(lambda: _row(db, login_id)["status"] == "waiting")
    return cid, login_id, worker, _home(clis)


def test_disabled_mid_login_is_refused_and_stays_disabled(db, clis, events):
    cid, login_id, worker, home = _codex_waiting(db, clis)
    with db.cursor() as cur:
        cur.execute("UPDATE credential SET enabled = FALSE WHERE id = %s", (cid,))
    (home / "go").touch()
    worker.join(20)
    _assert_terminal(_row(db, login_id), "failed", "disabled")
    stored = _stored(db, cid)
    assert not stored.enabled and stored.credentials["subscription_key"] == "old-at"
    events.dispatch.assert_not_called()


def test_a_live_lease_is_busy(db, clis, events):
    cid, login_id, worker, home = _codex_waiting(db, clis)
    with db.cursor() as cur:
        cur.execute("UPDATE credential SET lease_owner = 'job-1-attempt-1', "
                    "leased_until = UTC_TIMESTAMP() + INTERVAL 10 MINUTE WHERE id = %s", (cid,))
    (home / "go").touch()
    worker.join(20)
    _assert_terminal(_row(db, login_id), "failed", "busy")
    assert _stored(db, cid).credentials["subscription_key"] == "old-at"
    events.dispatch.assert_not_called()


def test_expiry_stops_the_login_and_kills_the_cli(db, clis):
    _cid, login_id, worker, home = _codex_waiting(db, clis)
    pid = int((home / "cli.pid").read_text())
    with db.cursor() as cur:
        cur.execute("UPDATE credential_login SET expires_at = UTC_TIMESTAMP() - INTERVAL 1 SECOND "
                    "WHERE id = %s", (login_id,))
    worker.join(20)
    _assert_terminal(_row(db, login_id), "failed", "expired")
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not home.exists()


def test_cancel_stops_the_worker(db, clis, events):
    _cid, login_id, worker, home = _codex_waiting(db, clis)
    assert _web(admin_api.cancel_credential_login, "POST", id=login_id).status == 204
    worker.join(20)
    _assert_terminal(_row(db, login_id), "cancelled")
    assert not home.exists()
    assert _web(admin_api.cancel_credential_login, "POST", id=999999999).status == 404


def test_a_cancel_during_starting_wins(db, clis, events):
    cid = _credential(db, "codex")
    login_id = _login(db, cid)
    conn = _test_connection()
    try:
        assert credential_login.claim(conn, login_id)
        assert _web(admin_api.cancel_credential_login, "POST", id=login_id).status == 204
        credential_login.run_login(conn, login_id, logging.getLogger("cl-test"), clis)
    finally:
        conn.close()
    _assert_terminal(_row(db, login_id), "cancelled")
    assert _row(db, login_id)["verify_url"] is None
    assert not any(clis.iterdir())


def test_a_cancel_just_before_the_save_wins(db, clis, events, monkeypatch):
    real = credential_login.credentials_from_auth

    def cancel_then(result):
        _web(admin_api.cancel_credential_login, "POST", id=login_id)
        return real(result)

    monkeypatch.setattr(credential_login, "credentials_from_auth", cancel_then)
    cid, login_id, worker, home = _codex_waiting(db, clis)
    (home / "go").touch()
    worker.join(20)
    _assert_terminal(_row(db, login_id), "cancelled")
    assert _stored(db, cid).credentials["subscription_key"] == "old-at"
    events.dispatch.assert_not_called()


@pytest.mark.parametrize("status", ["starting", "waiting", "verifying"])
def test_a_stale_heartbeat_is_swept_to_abandoned(db, tmp_path, status):
    login_id = _login(db, _credential(db, "claude"))
    with db.cursor() as cur:
        cur.execute("UPDATE credential_login SET status = %s, code_key = 'pem', code_box = 'box', "
                    "heartbeat_at = UTC_TIMESTAMP() - INTERVAL 31 SECOND WHERE id = %s", (status, login_id))
    conn = _test_connection()
    try:
        credential_login.sweep(conn, tmp_path)
    finally:
        conn.close()
    _assert_terminal(_row(db, login_id), "failed", "abandoned")


def test_sweep_expires_pending_and_keeps_live_rows(db, tmp_path):
    expired = _login(db, _credential(db, "claude", label="cl-a"))
    live = _login(db, _credential(db, "claude", label="cl-b"))
    with db.cursor() as cur:
        cur.execute("UPDATE credential_login SET expires_at = UTC_TIMESTAMP() - INTERVAL 1 SECOND "
                    "WHERE id = %s", (expired,))
    (tmp_path / f"{expired}-abc").mkdir()
    (tmp_path / f"{live}-def").mkdir()
    conn = _test_connection()
    try:
        credential_login.sweep(conn, tmp_path)
    finally:
        conn.close()
    _assert_terminal(_row(db, expired), "failed", "expired")
    assert _row(db, live)["status"] == "pending"
    assert [p.name for p in tmp_path.iterdir()] == [f"{live}-def"]


def test_a_sigkilled_worker_leaves_no_cli_and_the_sweep_removes_its_home(db, clis):
    cid = _credential(db, "codex")
    login_id = _login(db, cid)
    script = textwrap.dedent(f"""\
        import logging
        from pathlib import Path
        import agento.framework.harness as harness
        from agento.framework.agent_manager import credential_login
        from agento.modules.codex.src.auth import CodexCredentialAuthenticator
        from tests.integration.conftest import _test_connection
        harness.get_authenticator = lambda scope: CodexCredentialAuthenticator()
        credential_login.HEARTBEAT_S = 0.05
        conn = _test_connection()
        assert credential_login.claim(conn, {login_id})
        credential_login.run_login(conn, {login_id}, logging.getLogger("x"), Path({str(clis)!r}))
    """)
    worker = subprocess.Popen([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
                              env={**os.environ})
    try:
        _wait(lambda: _row(db, login_id)["status"] == "waiting")
        home = _home(clis)
        pid = int((home / "cli.pid").read_text())
        worker.send_signal(signal.SIGKILL)
        worker.wait(10)
        _wait(lambda: _gone(pid), timeout=10)
    finally:
        worker.kill()
    with db.cursor() as cur:
        cur.execute("UPDATE credential_login SET heartbeat_at = UTC_TIMESTAMP() - INTERVAL 31 SECOND "
                    "WHERE id = %s", (login_id,))
    conn = _test_connection()
    try:
        credential_login.sweep(conn, clis)
    finally:
        conn.close()
    _assert_terminal(_row(db, login_id), "failed", "abandoned")
    assert not home.exists()


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _run_claimed(login_id, tmp_root, before=None):
    """Claim ``login_id``, run ``before`` (a race) and then the login, on one worker connection."""
    conn = _test_connection()
    try:
        assert credential_login.claim(conn, login_id)
        if before:
            before()
        credential_login.run_login(conn, login_id, logging.getLogger("cl-test"), tmp_root)
    finally:
        conn.close()


def _age_heartbeat_and_sweep(login_id, tmp_root):
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE credential_login SET heartbeat_at = UTC_TIMESTAMP() - INTERVAL 31 SECOND "
                        "WHERE id = %s", (login_id,))
        credential_login.sweep(conn, tmp_root)
    finally:
        conn.close()


def _assert_nothing_saved(db, cid, events):
    assert _stored(db, cid).credentials["subscription_key"] == "old-at"
    events.dispatch.assert_not_called()


def test_a_sweep_during_starting_wins(db, clis, events):
    cid = _credential(db, "codex")
    login_id = _login(db, cid)
    _run_claimed(login_id, clis, before=lambda: _age_heartbeat_and_sweep(login_id, clis))
    _assert_terminal(_row(db, login_id), "failed", "abandoned")
    assert _row(db, login_id)["verify_url"] is None
    assert not any(clis.iterdir())
    _assert_nothing_saved(db, cid, events)


def test_a_sweep_just_before_the_save_wins(db, clis, events, monkeypatch):
    real = credential_login.credentials_from_auth

    def sweep_then(result):
        _age_heartbeat_and_sweep(login_id, clis)
        return real(result)

    monkeypatch.setattr(credential_login, "credentials_from_auth", sweep_then)
    cid, login_id, worker, home = _codex_waiting(db, clis)
    (home / "go").touch()
    worker.join(20)
    _assert_terminal(_row(db, login_id), "failed", "abandoned")
    assert not home.exists()
    _assert_nothing_saved(db, cid, events)


def test_a_login_that_expires_while_the_save_waits_for_the_credential_lock_is_not_saved(
        db, clis, events, monkeypatch):
    """The lock delay starts inside ``_save`` (its ``credentials_from_auth`` call), so the
    heartbeat and ``_watch`` expiry checks are already behind the worker."""
    real = credential_login.credentials_from_auth
    reached: list[bool] = []

    def lock_then(result):
        reached.append(True)
        holder = _test_connection()
        with holder.cursor() as cur:
            cur.execute("SELECT id FROM credential WHERE id = %s FOR UPDATE", (cid,))

        def expire_then_release():
            time.sleep(0.5)  # the save is now waiting for the credential row
            with db.cursor() as cur:
                cur.execute("UPDATE credential_login SET expires_at = UTC_TIMESTAMP() - INTERVAL 1 SECOND "
                            "WHERE id = %s", (login_id,))
            holder.commit()
            holder.close()

        threading.Thread(target=expire_then_release).start()
        return real(result)

    monkeypatch.setattr(credential_login, "credentials_from_auth", lock_then)
    cid, login_id, worker, home = _codex_waiting(db, clis)
    (home / "go").touch()
    worker.join(20)
    assert reached == [True]
    _assert_terminal(_row(db, login_id), "failed", "expired")
    assert not home.exists()
    _assert_nothing_saved(db, cid, events)


def test_an_authenticator_without_start_web_login_is_unsupported(db, clis, events, monkeypatch):
    monkeypatch.setattr("agento.framework.harness.get_authenticator", lambda scope: object())
    cid = _credential(db, "claude")
    login_id = _login(db, cid)
    _run_claimed(login_id, clis)
    _assert_terminal(_row(db, login_id), "failed", "unsupported")
    assert not clis.exists()  # no temp HOME was made
    _assert_nothing_saved(db, cid, events)


def test_a_login_url_that_is_not_https_is_bad_url(db, clis, events, monkeypatch):
    from agento.framework.harness import LoginPrompt

    closed = []

    class HttpLogin:
        prompt = LoginPrompt(url="http://login.example.com/x", user_code=None, needs_code=True)

        def close(self):
            closed.append(True)

    class HttpAuthenticator:
        def start_web_login(self, tmp_home, logger):
            return HttpLogin()

    monkeypatch.setattr("agento.framework.harness.get_authenticator", lambda scope: HttpAuthenticator())
    cid = _credential(db, "claude")
    login_id = _login(db, cid)
    _run_claimed(login_id, clis)
    _assert_terminal(_row(db, login_id), "failed", "bad_url")
    assert _row(db, login_id)["verify_url"] is None and closed == [True]
    assert not any(clis.iterdir())
    _assert_nothing_saved(db, cid, events)


def test_a_cli_that_does_not_start_is_cli_failed(db, clis, events, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no `claude` to run
    cid = _credential(db, "claude")
    login_id = _login(db, cid)
    _run_claimed(login_id, clis)
    _assert_terminal(_row(db, login_id), "failed", "cli_failed")
    assert not any(clis.iterdir())
    _assert_nothing_saved(db, cid, events)


def test_a_wrong_code_is_cli_failed(db, clis, events):
    cid = _credential(db, "claude")
    login_id = _login(db, cid)
    worker = _start_worker(login_id, clis, logging.getLogger("cl-test"))
    _wait(lambda: _row(db, login_id)["status"] == "waiting")
    assert _web(admin_api.credential_login_code, "POST", {"code": "wrong"}, id=login_id).status == 204
    worker.join(20)
    _assert_terminal(_row(db, login_id), "failed", "cli_failed")
    assert not any(clis.iterdir())
    _assert_nothing_saved(db, cid, events)


def test_the_worker_claims_no_login_after_its_budget(db, tmp_path, monkeypatch):
    first = _login(db, _credential(db, "claude", label="cl-a"))
    second = _login(db, _credential(db, "claude", label="cl-b"))
    ran: list[int] = []

    def slow_login(conn, login_id, logger, tmp_root):
        ran.append(login_id)
        time.sleep(0.3)  # longer than the budget

    monkeypatch.setattr(credential_login, "run_login", slow_login)
    conn = _test_connection()
    try:
        credential_login.run_worker(conn, logging.getLogger("cl-test"), tmp_path, budget_s=0.1)
    finally:
        conn.close()
    assert ran == [first]
    assert _row(db, second)["status"] == "pending"


def test_a_cancel_during_a_save_does_not_deadlock(db):
    """The save locks the credential row, then the login row. A cancel that wrote the login
    row first would then wait on the credential through the FK index (``status`` is in it):
    a lock cycle, error 1213 for one side."""
    cid = _credential(db, "claude")
    login_id = _login(db, cid)
    saver = _test_connection()
    errors: list[Exception] = []

    def cancel():
        conn = _test_connection()
        try:
            credential_login.cancel_login(conn, login_id)
        except Exception as exc:  # the test reports it
            errors.append(exc)
        finally:
            conn.close()

    try:
        with saver.cursor() as cur:
            cur.execute("SELECT id FROM credential WHERE id = %s FOR UPDATE", (cid,))  # as `_save` does
            t = threading.Thread(target=cancel)
            t.start()
            time.sleep(0.5)  # the cancel is now waiting
            cur.execute("SELECT status FROM credential_login WHERE id = %s FOR UPDATE", (login_id,))
        saver.commit()
    except Exception as exc:  # the test reports it
        errors.append(exc)
    finally:
        saver.close()
    t.join(20)
    assert errors == []
    assert _row(db, login_id)["status"] == "cancelled"


class _Recorder:
    """A connection that records the statements of each transaction."""

    def __init__(self, conn):
        self._conn = conn
        self.txs: list[list[str]] = [[]]

    def cursor(self):
        return _RecordingCursor(self._conn.cursor(), self.txs)

    def commit(self):
        self._conn.commit()
        self.txs.append([])

    def rollback(self):
        self._conn.rollback()
        self.txs.append([])


class _RecordingCursor:
    def __init__(self, cur, txs):
        self._cur, self._txs = cur, txs

    def execute(self, sql, args=()):
        self._txs[-1].append(" ".join(sql.split()))
        return self._cur.execute(sql, args)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._cur.close()

    def __getattr__(self, name):
        return getattr(self._cur, name)


_CREDENTIAL_LOCK = re.compile(r"FROM credential WHERE id (=|IN) .*FOR UPDATE")
# A DELETE of a child row takes no lock on the parent, so it is not in the class.
_LOGIN_WRITE = re.compile(r"^(UPDATE credential_login|INSERT INTO credential_login)|FROM credential_login .*FOR UPDATE")


def test_every_login_write_locks_the_credential_row_first(db, events, tmp_path):
    """Class guard: one lock order on every path, credential row then login row."""
    from cryptography.hazmat.primitives import serialization

    from agento.framework.agent_manager.auth import AuthResult

    log = logging.getLogger("cl-test")
    cid = _credential(db, "claude")
    raw = _test_connection()
    rec = _Recorder(raw)
    try:
        login_id = credential_login.request_login(rec, cid, None, {"claude"})
        assert credential_login.claim(rec, login_id)
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        pem = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        assert credential_login._fenced(rec, login_id, ("starting",), "status = 'waiting', needs_code = TRUE, "
                                        "code_key = %s, heartbeat_at = UTC_TIMESTAMP()", (pem,))
        assert credential_login._heartbeat(rec, login_id)["status"] == "waiting"
        assert credential_login.put_code(rec, login_id, CODE) is None
        assert credential_login._take_code(rec, login_id, key, log) == CODE
        credential_login._save(rec, login_id, cid, AuthResult(subscription_key="new-at"), needs_code=True,
                               logger=log)
        assert _row(db, login_id)["status"] == "done"

        cancelled = credential_login.request_login(rec, cid, None, {"claude"})
        assert credential_login.cancel_login(rec, cancelled)
        failed = credential_login.request_login(rec, cid, None, {"claude"})
        credential_login._finish(rec, failed, "cli_failed", log)
        stale = credential_login.request_login(rec, cid, None, {"claude"})
        with db.cursor() as cur:
            cur.execute("UPDATE credential_login SET status = 'waiting', heartbeat_at = UTC_TIMESTAMP() - "
                        "INTERVAL 31 SECOND WHERE id = %s", (stale,))
        credential_login.sweep(rec, tmp_path)
        assert _row(db, stale)["error_code"] == "abandoned"
    finally:
        raw.close()

    unlocked = [(tx, sql) for tx in rec.txs for i, sql in enumerate(tx)
                if _LOGIN_WRITE.search(sql) and not any(_CREDENTIAL_LOCK.search(s) for s in tx[:i])]
    assert unlocked == []


def test_the_sweep_keeps_the_home_of_a_login_that_starts_while_it_runs(db, tmp_path, monkeypatch):
    """A login claimed after the sweep's first read (its transaction snapshot) makes its HOME
    before the sweep removes homes: the sweep must not see it as finished."""
    real = credential_login.mark_stale
    started: list[int] = []

    def mark_then_start(conn, credential_id=None):
        real(conn, credential_id)
        if credential_id is None and not started:
            login_id = _login(db, _credential(db, "claude", label="cl-new"))
            other = _test_connection()
            try:
                assert credential_login.claim(other, login_id)
            finally:
                other.close()
            (tmp_path / f"{login_id}-new").mkdir()  # what `run_login` does after the claim
            started.append(login_id)

    monkeypatch.setattr(credential_login, "mark_stale", mark_then_start)
    conn = _test_connection()
    try:
        credential_login.sweep(conn, tmp_path)
    finally:
        conn.close()
    assert (tmp_path / f"{started[0]}-new").is_dir()
