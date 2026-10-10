"""``credential:limits`` against real MySQL with a fake authenticator, and the panel read."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from agento.framework.access import accounts, sessions
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
    _clean(c)
    authenticators = {"lim": FakeAuthenticator(), "old": NoLimitsAuthenticator()}
    monkeypatch.setattr("agento.framework.harness.get_authenticator", authenticators.get)
    yield c
    _clean(c)
    c.close()


def _clean(c):
    with c.cursor() as cur:
        cur.execute("DELETE FROM credential WHERE label LIKE 'lim-%%'")


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
