"""The /api/admin/* read payloads against real MySQL: the shape the panel renders."""
from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pymysql
import pytest

from agento.framework.access import accounts, sessions
from agento.framework.agent_manager.models import encrypt_credentials
from agento.web import admin_api, api

from .conftest import _test_connection

ADMIN = accounts.User(id=1, username="root", role="admin", is_active=True)


@pytest.fixture
def db():
    c = _test_connection(autocommit=True)
    _clean(c)
    with c.cursor() as cur:
        cur.execute("INSERT INTO workspace (code, label) VALUES ('adm-ws', 'Admin WS')")
        ws = cur.lastrowid
        cur.execute("INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, 'adm-av', 'Admin AV')", (ws,))
        av = cur.lastrowid
    yield c, ws, av
    _clean(c)
    c.close()


def _clean(c):
    with c.cursor() as cur:
        cur.execute("DELETE FROM job WHERE idempotency_key LIKE 'adm:%%'")
        cur.execute("DELETE FROM usage_log WHERE reference_id = 'adm'")
        cur.execute("DELETE FROM credential WHERE label LIKE 'adm-%%'")
        cur.execute("DELETE FROM core_config_data WHERE path = 'jira/jira_token' AND scope != 'default'")
        cur.execute("DELETE FROM agent_view WHERE code = 'adm-av'")
        cur.execute("DELETE FROM workspace WHERE code = 'adm-ws'")


def _get(conn, handler, query=None, **params) -> api.Response:
    session = sessions.Session(id="sid", user=ADMIN,
                               expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))
    req = api.Request(method="GET", path="/", headers={}, body=b"", cookies={}, origins=None,
                      params=params, query=query or {}, conn=conn, session=session)
    resp = handler(req)
    # The server sends json.dumps(body): a DB value it cannot encode (Decimal from SUM) is a 500.
    json.dumps(resp.body)
    return resp


def test_jobs_and_job_detail_payload(db):
    conn, _ws, av = db
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job (type, source, agent_view_id, reference_id, idempotency_key, status, prompt, "
            "started_at, finished_at) VALUES ('todo', 'jira', %s, 'AI-7', 'adm:1', 'FAILED', %s, "
            "UTC_TIMESTAMP(), UTC_TIMESTAMP())",
            (av, "p" * 700),
        )
        job_id = cur.lastrowid

    rows = _get(conn, admin_api.jobs, {"status": "FAILED"}).body
    row = next(r for r in rows if r["id"] == job_id)
    assert set(row) == {"id", "type", "status", "source", "reference_id", "agent_type", "agent_view_code",
                        "created_at", "started_at", "finished_at", "input_tokens", "output_tokens", "error_class"}
    assert (row["agent_view_code"], row["status"]) == ("adm-av", "FAILED")
    assert row["created_at"].endswith("Z")

    detail = _get(conn, admin_api.job_detail, id=str(job_id)).body
    assert detail["prompt"] == "p" * 500 + "..."
    assert {"output", "result_summary", "error_message", "model"} <= set(detail)


def test_agents_payload(db):
    conn, _ws, av = db
    rows = _get(conn, admin_api.agents).body
    assert next(r for r in rows if r["id"] == av) == {
        "id": av, "code": "adm-av", "label": "Admin AV", "workspace_code": "adm-ws",
        "ingress_count": 0, "build_status": "none",
    }


@contextlib.contextmanager
def _no_decrypt():
    """Both lookup sites, whichever backend an earlier test registered with set_encryptor()."""
    boom = AssertionError("decrypted")
    with patch("agento.framework.encryptor.get_encryptor", side_effect=boom), \
         patch("agento.framework.config_resolver.get_encryptor", side_effect=boom):
        yield


def _credentials(conn, n: int) -> list[int]:
    ids = []
    payload = encrypt_credentials({"access_token": "sk-adm-secret", "refresh_token": "rt-adm-secret"})
    with conn.cursor() as cur:
        for i in range(n):
            cur.execute(
                "INSERT INTO credential (agent_type, scope, type, label, credentials, token_limit, enabled, "
                "status, error_msg, error_source) VALUES ('adm', 'adm', 'oauth', %s, %s, 1000, %s, 'error', "
                "'stderr: sk-adm-secret', 'auto')",
                (f"adm-{len(ids)}-{n}", payload, i % 2 == 0),
            )
            ids.append(cur.lastrowid)
        cur.execute("INSERT INTO usage_log (credential_id, tokens_used, input_tokens, output_tokens, reference_id) "
                    "VALUES (%s, 100, 60, 40, 'adm')", (ids[0],))
    return ids


@pytest.fixture
def selects(monkeypatch):
    seen: list[str] = []
    real = pymysql.cursors.DictCursor.execute

    def spy(self, query, args=None):
        if query.lstrip().upper().startswith("SELECT"):
            seen.append(query)
        return real(self, query, args)

    monkeypatch.setattr(pymysql.cursors.DictCursor, "execute", spy)
    return seen


@pytest.mark.parametrize("n", [1, 5])
def test_credentials_cost_two_reads_and_decrypt_nothing(db, selects, n):
    conn = db[0]
    ids = _credentials(conn, n)
    selects.clear()
    with _no_decrypt():
        rows = _get(conn, admin_api.credentials).body
    assert len(selects) == 2
    mine = [r for r in rows if r["id"] in ids]
    # A disabled credential and one with no usage are still listed.
    assert len(mine) == n and {r["enabled"] for r in mine} == ({True} if n == 1 else {True, False})
    assert (mine[0]["tokens_used"], mine[0]["call_count"]) == (100, 1)
    assert all(r["tokens_used"] == 0 for r in mine[1:])
    assert "secret" not in str(rows)


def test_a_stored_secret_is_set_inherited_and_never_decrypted(db, monkeypatch):
    from agento.framework.admin import data

    conn, ws, av = db
    monkeypatch.setattr("agento.framework.module_status.read_module_status", lambda *a, **kw: {})
    data.clear_module_schema_cache()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO core_config_data (scope, scope_id, path, value, encrypted) "
                    "VALUES ('workspace', %s, 'jira/jira_token', 'not-even-ciphertext', 1)", (ws,))
    try:
        with _no_decrypt():
            at_ws = _get(conn, admin_api.get_config, {"module": "jira", "scope": "workspace", "scope_id": str(ws)})
            at_av = _get(conn, admin_api.get_config, {"module": "jira", "scope": "agent_view", "scope_id": str(av)})
    finally:
        data.clear_module_schema_cache()

    def token(resp):
        return next(f for f in resp.body if f["path"] == "jira/jira_token")

    assert {k: token(at_ws)[k] for k in ("secret", "is_set", "source")} == {"secret": True, "is_set": True,
                                                                            "source": "db"}
    assert (token(at_av)["source"], token(at_av)["is_set"]) == ("db:inherited", True)
    assert "value" not in token(at_ws) and "not-even-ciphertext" not in str(at_ws.body)
