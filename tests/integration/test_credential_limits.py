"""``credential:limits`` against real MySQL with a fake authenticator, and the panel read."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agento.framework.access import accounts, sessions
from agento.framework.agent_manager.errors import TransientAuthError
from agento.framework.agent_manager.models import encrypt_credentials
from agento.framework.cli.credential import refresh_credential_limits
from agento.framework.harness import CredentialLimits, LimitWindow
from agento.web import admin_api, api

from .conftest import _test_connection

ADMIN = accounts.User(id=1, username="root", role="admin", is_active=True)
RESET = datetime(2026, 10, 4, 15, tzinfo=UTC)
# What a vendor reader may pass on from a malformed answer.
BAD = {
    "nan-window": CredentialLimits(windows=(LimitWindow("5h", float("nan"), None),)),
    "inf-window": CredentialLimits(windows=(LimitWindow("5h", float("inf"), None),)),
    "over-100": CredentialLimits(windows=(LimitWindow("5h", 100.5, None),)),
    "below-0": CredentialLimits(windows=(LimitWindow("5h", -1.0, None),)),
    "nan-balance": CredentialLimits(balance_usd=float("nan")),
    "inf-balance": CredentialLimits(balance_usd=float("-inf")),
}


class FakeAuthenticator:
    def fetch_limits(self, credentials: dict, credential_type: str):
        token = credentials["subscription_key"]
        if token == "boom":
            raise RuntimeError(f"401 for {token}")
        if credential_type != "oauth":
            return None
        if token in BAD:
            return BAD[token]
        return CredentialLimits(windows=(LimitWindow("5h", 25.0, RESET), LimitWindow("Week", 60.5, None)))


class NoLimitsAuthenticator:
    """An out-of-tree authenticator without the optional member."""


@pytest.fixture
def db(monkeypatch):
    c = _test_connection(autocommit=True)
    authenticators = {"lim": FakeAuthenticator(), "old": NoLimitsAuthenticator()}
    monkeypatch.setattr("agento.framework.harness.get_authenticator", authenticators.get)
    yield c
    c.close()


def _insert(c, label, *, scope="lim", token="sk-lim-secret", type_="oauth", enabled=True, status="ok", expires=None):
    with c.cursor() as cur:
        cur.execute(
            "INSERT INTO credential (agent_type, scope, type, label, credentials, enabled, status, expires_at, "
            "limits, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '{\"balance_usd\": 1}', "
            "'2026-01-01 00:00:00')",
            (scope, scope, type_, label, encrypt_credentials({"subscription_key": token}), enabled, status, expires),
        )
        return cur.lastrowid


def _limits(c, cid):
    with c.cursor() as cur:
        cur.execute("SELECT limits, limits_at, updated_at FROM credential WHERE id = %s", (cid,))
        return cur.fetchone()


def test_stores_nulls_and_skips(db, caplog):
    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    ok = _insert(db, "lim-ok")
    failing = _insert(db, "lim-boom", token="boom")
    api_key = _insert(db, "lim-key", type_="other_api_key")
    no_member = _insert(db, "lim-old", scope="old")
    disabled = _insert(db, "lim-off", enabled=False)
    errored = _insert(db, "lim-err", status="error")
    expired = _insert(db, "lim-exp", expires=past)

    caplog.set_level(logging.DEBUG)
    conn = _test_connection()
    try:
        refresh_credential_limits(conn, logging.getLogger("lim-test"))
    finally:
        conn.close()

    row = _limits(db, ok)
    assert json.loads(row["limits"]) == {
        "windows": [{"label": "5h", "used_pct": 25.0, "resets_at": "2026-10-04T15:00:00Z"},
                    {"label": "Week", "used_pct": 60.5, "resets_at": None}],
        "balance_usd": None,
    }
    assert row["limits_at"] is not None and row["updated_at"] == datetime(2026, 1, 1)
    # A failed check is NULL with a check time; nothing to show is NULL with none.
    assert (_limits(db, failing)["limits"], _limits(db, failing)["limits_at"] is not None) == (None, True)
    for cid in (api_key, no_member):
        assert (_limits(db, cid)["limits"], _limits(db, cid)["limits_at"]) == (None, None)
    for cid in (disabled, errored, expired):
        assert _limits(db, cid)["limits_at"] is None  # skipped: the old value stays
    assert f"id={failing}" in caplog.text and "RuntimeError" in caplog.text
    assert "boom" not in caplog.text and "sk-lim-secret" not in caplog.text


def test_the_panel_reads_limits_without_the_payload(db):
    cid = _insert(db, "lim-ok")
    conn = _test_connection()
    try:
        refresh_credential_limits(conn, logging.getLogger("lim-test"))
    finally:
        conn.close()
    session = sessions.Session(id="sid", user=ADMIN,
                               expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))
    req = api.Request(method="GET", path="/", headers={}, body=b"", cookies={}, origins=None, conn=db,
                      session=session)
    row = next(r for r in admin_api.credentials(req).body if r["id"] == cid)
    assert row["type"] == "oauth" and row["limits"]["windows"][0]["label"] == "5h"
    assert row["limits_at"].endswith("Z")
    assert "sk-lim-secret" not in json.dumps(row)


def test_one_credential_id_reads_only_that_row_and_only_when_eligible(db):
    target = _insert(db, "lim-one")
    other = _insert(db, "lim-other")
    disabled = _insert(db, "lim-one-off", enabled=False)
    conn = _test_connection()
    try:
        for cid in (target, disabled, 999999999):  # the last one is gone
            refresh_credential_limits(conn, logging.getLogger("lim-test"), credential_id=cid)
    finally:
        conn.close()
    assert _limits(db, target)["limits_at"] is not None
    assert _limits(db, other)["limits_at"] is None
    assert _limits(db, disabled)["limits_at"] is None


@pytest.mark.parametrize("bad", sorted(BAD))
def test_a_malformed_value_stores_no_limits_and_the_next_credential_still_refreshes(db, caplog, bad):
    first = _insert(db, "lim-bad", token=bad)
    after = _insert(db, "lim-ok")
    conn = _test_connection()
    try:
        refresh_credential_limits(conn, logging.getLogger("lim-test"))
    finally:
        conn.close()
    assert (_limits(db, first)["limits"], _limits(db, first)["limits_at"] is not None) == (None, True)
    assert json.loads(_limits(db, after)["limits"])["windows"][0]["label"] == "5h"
    assert f"id={first}" in caplog.text and "ValueError" in caplog.text


def test_a_token_the_harness_reports_expired_is_skipped(db, monkeypatch):
    """Codex keeps no ``expires_at``: the owning harness's TTL decides, as for the resolver."""
    class Adapter:
        def credential_ttl_seconds(self, credential):
            return -5 if credential.label == "lim-ttl-gone" else 3600

    class Owner:
        class adapter:
            workspace_adapter = Adapter()

    monkeypatch.setattr("agento.framework.harness.registry.get_harness_for_scope", lambda scope: Owner())
    gone = _insert(db, "lim-ttl-gone")
    live = _insert(db, "lim-ttl-live")
    conn = _test_connection()
    try:
        refresh_credential_limits(conn, logging.getLogger("lim-test"))
    finally:
        conn.close()
    assert _limits(db, gone)["limits_at"] is None  # skipped: the last result stays
    assert _limits(db, live)["limits_at"] is not None


class RenewingAdapter:
    """A harness whose payload carries its own TTL (``ttl``); ``refresh_credentials``
    persists a fresh token unless the test says otherwise, and records the lease it ran under."""

    def __init__(self, db, *, result=True, raises=None, during=None):
        self.db, self.result, self.raises, self.during = db, result, raises, during
        self.calls = []

    def credential_ttl_seconds(self, credential):
        return (credential.credentials or {}).get("ttl", 3600)

    def refresh_credentials(self, credential, conn):
        from agento.framework.agent_manager.credential_store import update_refreshed_credentials

        with self.db.cursor() as cur:
            cur.execute("SELECT lease_owner, leased_until FROM credential WHERE id = %s", (credential.id,))
            self.calls.append(cur.fetchone())
        if self.during:
            self.during(self.db, credential.id)
        if self.raises:
            raise self.raises
        if self.result:
            update_refreshed_credentials(
                conn, credential.id, {"subscription_key": "sk-lim-new", "refresh_token": "R1", "ttl": 3600},
            )
        return self.result


@pytest.fixture
def renewing(db, monkeypatch):
    def install(**kw):
        adapter = RenewingAdapter(db, **kw)
        owner = SimpleNamespace(adapter=SimpleNamespace(workspace_adapter=adapter))
        monkeypatch.setattr("agento.framework.harness.registry.get_harness_for_scope", lambda scope: owner)
        return adapter
    return install


class TokenAuthenticator:
    """Answers with the token it was given, so a test sees which payload was read."""

    def fetch_limits(self, credentials, credential_type):
        self.tokens = [*getattr(self, "tokens", []), credentials["subscription_key"]]
        return CredentialLimits(windows=(LimitWindow("5h", 10.0, None),))


def _insert_expired(c, label):
    cid = _insert(c, label)
    with c.cursor() as cur:
        cur.execute(
            "UPDATE credential SET credentials = %s WHERE id = %s",
            (encrypt_credentials({"subscription_key": "sk-lim-secret", "refresh_token": "R0", "ttl": -5}), cid),
        )
    return cid


def _run(monkeypatch):
    auth = TokenAuthenticator()
    monkeypatch.setattr("agento.framework.harness.get_authenticator", lambda scope: auth)
    conn = _test_connection()
    try:
        refresh_credential_limits(conn, logging.getLogger("lim-test"))
    finally:
        conn.close()
    return auth


def _lease(c, cid):
    with c.cursor() as cur:
        cur.execute("SELECT lease_owner, leased_until FROM credential WHERE id = %s", (cid,))
        return cur.fetchone()


def test_an_expired_token_is_renewed_under_a_lease_then_released(db, renewing, monkeypatch):
    adapter = renewing()
    cid = _insert_expired(db, "lim-renew")
    auth = _run(monkeypatch)
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["lease_owner"].startswith(f"limits-{cid}-")
    assert adapter.calls[0]["leased_until"] is not None
    assert _lease(db, cid) == {"lease_owner": None, "leased_until": None}
    assert auth.tokens == ["sk-lim-new"]
    assert _limits(db, cid)["limits_at"] is not None


@pytest.mark.parametrize("kw", [
    {"result": False},
    {"raises": TransientAuthError("could not be refreshed: sk-lim-secret")},
])
def test_a_refresh_that_does_not_renew_keeps_the_last_result(db, renewing, monkeypatch, caplog, kw):
    adapter = renewing(**kw)
    cid = _insert_expired(db, "lim-stuck")
    auth = _run(monkeypatch)
    assert len(adapter.calls) == 1
    assert _lease(db, cid) == {"lease_owner": None, "leased_until": None}
    assert getattr(auth, "tokens", []) == []
    assert _limits(db, cid)["limits_at"] is None
    if "raises" in kw:
        assert f"id={cid}" in caplog.text and "TransientAuthError" in caplog.text
        assert "sk-lim-secret" not in caplog.text and "could not be refreshed" not in caplog.text


def test_a_lease_held_by_a_job_is_not_touched(db, renewing, monkeypatch):
    adapter = renewing()
    cid = _insert_expired(db, "lim-held")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE credential SET lease_owner = 'job-7-attempt-1', "
            "leased_until = UTC_TIMESTAMP() + INTERVAL 60 SECOND WHERE id = %s", (cid,),
        )
    auth = _run(monkeypatch)
    assert adapter.calls == [] and getattr(auth, "tokens", []) == []
    assert _lease(db, cid)["lease_owner"] == "job-7-attempt-1"
    assert _limits(db, cid)["limits_at"] is None


def test_a_row_a_job_rotated_meanwhile_is_read_not_refreshed(db, renewing, monkeypatch):
    """``list_credentials`` saw the old token; a job rotated it before the claim."""
    from agento.framework.agent_manager import credential_store

    adapter = renewing()
    cid = _insert_expired(db, "lim-raced")
    claim = credential_store.claim_credential_lease

    def claim_after_a_job(conn, *a):
        credential_store.update_refreshed_credentials(
            db, cid, {"subscription_key": "sk-lim-job", "refresh_token": "R1", "ttl": 3600},
        )
        return claim(conn, *a)

    monkeypatch.setattr(credential_store, "claim_credential_lease", claim_after_a_job)
    auth = _run(monkeypatch)
    assert adapter.calls == []
    assert auth.tokens == ["sk-lim-job"]
    assert _lease(db, cid) == {"lease_owner": None, "leased_until": None}


def _disable(c, cid):
    with c.cursor() as cur:
        cur.execute("UPDATE credential SET enabled = FALSE WHERE id = %s", (cid,))


def _quarantine(c, cid):
    with c.cursor() as cur:
        cur.execute("UPDATE credential SET status = 'error' WHERE id = %s", (cid,))


def _delete(c, cid):
    with c.cursor() as cur:
        cur.execute("DELETE FROM credential WHERE id = %s", (cid,))


@pytest.mark.parametrize("change", [_disable, _quarantine, _delete])
def test_a_row_made_ineligible_after_the_claim_is_not_refreshed(db, renewing, monkeypatch, change):
    from agento.framework.agent_manager import credential_store

    adapter = renewing()
    cid = _insert_expired(db, "lim-gone")
    claim = credential_store.claim_credential_lease

    def claim_then_operator_acts(conn, *a):
        held = claim(conn, *a)
        conn.commit()
        change(db, cid)
        return held

    monkeypatch.setattr(credential_store, "claim_credential_lease", claim_then_operator_acts)
    auth = _run(monkeypatch)
    assert adapter.calls == [] and getattr(auth, "tokens", []) == []
    if change is not _delete:
        assert _lease(db, cid) == {"lease_owner": None, "leased_until": None}
        assert _limits(db, cid)["limits_at"] is None


@pytest.mark.parametrize("change", [_disable, _quarantine])
def test_a_row_made_ineligible_during_the_refresh_is_not_read(db, renewing, monkeypatch, change):
    adapter = renewing(during=change)
    cid = _insert_expired(db, "lim-late")
    auth = _run(monkeypatch)
    assert len(adapter.calls) == 1
    assert getattr(auth, "tokens", []) == []
    assert _lease(db, cid) == {"lease_owner": None, "leased_until": None}
    assert _limits(db, cid)["limits_at"] is None


def test_a_rotation_is_committed_even_when_the_release_fails(db, renewing, monkeypatch):
    """The stored refresh token is spent once the CLI ran: losing the new one strands the credential."""
    from agento.framework.agent_manager import credential_store
    from agento.framework.agent_manager.credential_store import get_credential

    renewing()
    cid = _insert_expired(db, "lim-release-fails")

    def release_fails(*a, **kw):
        raise RuntimeError("lock wait timeout")

    monkeypatch.setattr(credential_store, "release_credential_lease", release_fails)
    with pytest.raises(RuntimeError, match="lock wait timeout"):
        _run(monkeypatch)
    assert get_credential(db, cid).credentials["subscription_key"] == "sk-lim-new"
