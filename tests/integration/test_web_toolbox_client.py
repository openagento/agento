"""A panel tool call mints one user_session capability and presents it as a Bearer header."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from agento.framework.access import accounts, launches, sessions
from agento.framework.toolbox_capability import token_hash
from agento.web.toolbox_client import invoke_launch_action, invoke_tool

from .conftest import _test_connection

URL = "http://toolbox:3001/internal/tools/versioned_artifact_get_current:invoke"


@pytest.fixture
def conn():
    c = _test_connection(autocommit=True)
    _clean(c)
    yield c
    _clean(c)
    c.close()


def _clean(c):
    with c.cursor() as cur:
        cur.execute("DELETE FROM toolbox_capability WHERE kind IN ('user_session', 'miniapp')")
        for table in ("launch", "session", "role_grant", "`user`"):
            cur.execute(f"DELETE FROM {table}")


@pytest.fixture
def scope(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES ('e2-tc', 'e2')")
        cur.execute("SELECT id FROM workspace WHERE code = 'e2-tc'")
        ws = cur.fetchone()["id"]
        cur.execute("INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, 'e2-tc-view', 'v')", (ws,))
        cur.execute("SELECT id FROM agent_view WHERE code = 'e2-tc-view'")
        return ws, cur.fetchone()["id"]


@pytest.fixture
def session(conn):
    user = accounts.create_user(conn, "alice", "user", "correct horse battery")
    return sessions.create_session(conn, user)[0]


@respx.mock
def test_bearer_header_and_the_row_it_minted(conn, scope, session):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True, "result": {}, "execution_id": "e"}))
    ws, view = scope
    result = invoke_tool(conn, session, "versioned_artifact_get_current", {"artifact_code": "a"},
                         workspace_id=ws, agent_view_id=view)
    assert result.status == 200 and result.body["ok"] is True
    request = route.calls.last.request
    assert request.url.query == b""
    token = request.headers["authorization"].removeprefix("Bearer ")
    assert token and "cap" not in str(request.url)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, source_kind, source_id, subject_id, workspace_id, agent_view_id,"
            " TIMESTAMPDIFF(SECOND, created_at, expires_at) AS life FROM toolbox_capability WHERE token_hash = %s",
            (token_hash(token),),
        )
        row = cur.fetchone()
    assert row["kind"] == "user_session" and row["source_kind"] == "session"
    assert row["source_id"] == session.id and row["subject_id"] == str(session.user.id)
    assert (row["workspace_id"], row["agent_view_id"]) == (ws, view)
    assert 0 < row["life"] <= 30


@respx.mock
def test_toolbox_status_passes_through(conn, scope, session):
    body = {"ok": False, "error": {"code": "not_found", "message": "tool not found"}, "execution_id": "e"}
    respx.post(URL).mock(return_value=httpx.Response(404, json=body))
    result = invoke_tool(conn, session, "versioned_artifact_get_current", {}, workspace_id=scope[0],
                         agent_view_id=scope[1])
    assert (result.status, result.body) == (404, body)


@respx.mock
@pytest.mark.parametrize("side_effect", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
def test_toolbox_unreachable_is_503(conn, scope, session, side_effect):
    respx.post(URL).mock(side_effect=side_effect)
    result = invoke_tool(conn, session, "versioned_artifact_get_current", {}, workspace_id=scope[0],
                         agent_view_id=scope[1])
    assert result.status == 503


def test_expired_session_mints_nothing(conn, scope, session):
    with conn.cursor() as cur:
        cur.execute("UPDATE session SET expires_at = NOW() - INTERVAL 1 SECOND WHERE id = %s", (session.id,))
    result = invoke_tool(conn, session, "versioned_artifact_get_current", {}, workspace_id=scope[0],
                         agent_view_id=scope[1])
    assert result.status == 401
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM toolbox_capability WHERE kind = 'user_session'")
        assert cur.fetchone()["n"] == 0


ACTION_URL = "http://toolbox:3001/internal/tools/notes_add:invoke"
V1 = "v-20260101-000000-aaaa"


@pytest.fixture
def launch(conn, scope, session, monkeypatch):
    """A redeemed launch of an activated version that allows `notes_add`."""
    ws, view = scope
    monkeypatch.setattr(launches, "has_operation", lambda *a: True)
    row, code = launches.create_launch(conn, session.user, workspace_id=ws, agent_view_id=view, artifact_code="app",
                                       version_id=V1, manifest_fingerprint="f" * 64, allowed_actions=["notes_add"])
    assert launches.redeem(conn, row.id, code)
    return row


@respx.mock
def test_launch_action_mints_a_miniapp_capability_bound_to_the_launch(conn, session, launch):
    route = respx.post(ACTION_URL).mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    result = invoke_launch_action(conn, session, launch.id, "notes_add", {"text": "hi"})
    assert result.status == 200
    token = route.calls.last.request.headers["authorization"].removeprefix("Bearer ")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, source_kind, source_id, subject_id, app_artifact_code, app_version_id, app_launch_id,"
            " tool_ceiling, TIMESTAMPDIFF(SECOND, created_at, expires_at) AS life"
            " FROM toolbox_capability WHERE token_hash = %s", (token_hash(token),))
        row = cur.fetchone()
    assert (row["kind"], row["source_kind"], row["source_id"]) == ("miniapp", "launch", launch.id)
    assert (row["app_artifact_code"], row["app_version_id"], row["app_launch_id"]) == ("app", V1, launch.id)
    assert json.loads(row["tool_ceiling"]) == ["notes_add"]
    assert row["subject_id"] == str(session.user.id) and 0 < row["life"] <= 30


@respx.mock
def test_launch_action_outside_the_allowed_actions_mints_nothing(conn, session, launch):
    respx.post(url__regex=r".*").mock(side_effect=AssertionError("must not call the toolbox"))
    assert invoke_launch_action(conn, session, launch.id, "notes_delete", {}).status == 403
    _assert_no_miniapp_row(conn)


@respx.mock
@pytest.mark.parametrize("state", ["revoked", "unredeemed", "other_user"])
def test_launch_action_on_a_launch_that_is_not_yours_and_live_is_404(conn, scope, session, launch, state):
    respx.post(url__regex=r".*").mock(side_effect=AssertionError("must not call the toolbox"))
    caller = session
    with conn.cursor() as cur:
        if state == "revoked":
            cur.execute("UPDATE launch SET revoked_at = NOW() WHERE id = %s", (launch.id,))
        elif state == "unredeemed":
            cur.execute("UPDATE launch SET exchange_redeemed_at = NULL WHERE id = %s", (launch.id,))
    if state == "other_user":
        bob = accounts.create_user(conn, "bob", "user", "correct horse battery")
        caller = sessions.create_session(conn, bob)[0]
    assert invoke_launch_action(conn, caller, launch.id, "notes_add", {}).status == 404
    _assert_no_miniapp_row(conn)


def _assert_no_miniapp_row(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM toolbox_capability WHERE kind = 'miniapp'")
        assert cur.fetchone()["n"] == 0
