"""A `miniapp` capability minted by web, verified by the toolbox's real verifier and `launch`
checker, against the real schema (PRD E6 §10). The Node side runs from
tests/integration/node/verify_capability.mjs; the SQL is the shared fixture
tests/fixtures/miniapp_sql_v1.json.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from agento.framework.access import accounts, launches, sessions
from agento.web.toolbox_client import invoke_launch_action

from .conftest import _test_connection

_ROOT = Path(__file__).resolve().parents[2]
_DRIVER = Path(__file__).parent / "node" / "verify_capability.mjs"
SQL = {k: v.replace("?", "%s") for k, v in
       json.loads((_ROOT / "tests" / "fixtures" / "miniapp_sql_v1.json").read_text()).items() if k.endswith("_sql")}
V1 = "v-20260101-000000-aaaa"
FP, OTHER_FP = "a" * 64, "b" * 64
TOOL = "versioned_artifact_get_current"
URL = f"http://toolbox:3001/internal/tools/{TOOL}:invoke"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (_ROOT / "src/agento/toolbox/node_modules/mysql2").is_dir(),
    reason="needs node and the toolbox's npm install")


@pytest.fixture
def conn():
    c = _test_connection(autocommit=True)
    _clean(c)
    yield c
    _clean(c)
    c.close()


def _clean(c):
    with c.cursor() as cur:
        cur.execute("DELETE FROM toolbox_capability WHERE kind = 'miniapp'")
        cur.execute("DELETE FROM miniapp_activation WHERE artifact_code = 'e6-chk'")
        for table in ("launch", "session", "role_grant", "`user`"):
            cur.execute(f"DELETE FROM {table}")


@pytest.fixture
def scope(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES ('e6-chk', 'e6')")
        cur.execute("SELECT id FROM workspace WHERE code = 'e6-chk'")
        ws = cur.fetchone()["id"]
        cur.execute("INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, 'e6-chk-view', 'v')", (ws,))
        cur.execute("SELECT id FROM agent_view WHERE code = 'e6-chk-view'")
        view = cur.fetchone()["id"]
    return ws, view


@pytest.fixture
def launched(conn, scope):
    """alice may launch in the view and call TOOL; e6-chk V1 is activated; a redeemed pinned launch."""
    ws, view = scope
    user = accounts.create_user(conn, "alice", "user", "correct horse battery")
    accounts.add_grant(conn, "user", "operation", "artifact.launch", agent_view_id=view)
    accounts.add_grant(conn, "user", "tool", TOOL, agent_view_id=view)
    with conn.cursor() as cur:
        cur.execute(SQL["activate_sql"], ("e6-chk", V1, FP, json.dumps([TOOL]), "admin"))
    launch, code = launches.create_launch(conn, user, workspace_id=ws, agent_view_id=view, artifact_code="e6-chk",
                                          version_id=V1, manifest_fingerprint=FP, allowed_actions=[TOOL])
    assert launches.redeem(conn, launch.id, code)
    return sessions.create_session(conn, user)[0], launch


@respx.mock
def _mint(conn, session, launch) -> str:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    assert invoke_launch_action(conn, session, launch.id, TOOL, {}).status == 200
    return route.calls.last.request.headers["authorization"].removeprefix("Bearer ")


def _verify(token: str):
    env = {**os.environ, "CORE_MODULES_DIR": str(_ROOT / "src/agento/modules")}
    out = subprocess.run(["node", str(_DRIVER)], input=json.dumps({"token": token, "endpoint": "invoke"}),
                         capture_output=True, text=True, env=env, timeout=30, check=True)
    return json.loads(out.stdout)


def test_the_toolbox_accepts_a_minted_miniapp_capability_with_its_ceiling_and_app(conn, launched):
    session, launch = launched
    verified = _verify(_mint(conn, session, launch))
    assert verified is not None
    ctx = verified["context"]
    assert (ctx["kind"], ctx["tool_ceiling"], verified["single_use"]) == ("miniapp", [TOOL], True)
    assert ctx["app"] == {"artifact_code": "e6-chk", "version_id": V1, "launch_id": launch.id}
    assert verified["permitted_tools"] == [TOOL]  # the role's grants for the scope


@pytest.mark.parametrize("change", ["deactivate", "other_fingerprint", "revoke_launch", "remove_operation"])
def test_the_toolbox_refuses_it_once_the_launch_or_activation_is_gone(conn, launched, change):
    session, launch = launched
    token = _mint(conn, session, launch)
    with conn.cursor() as cur:
        if change == "deactivate":
            cur.execute(SQL["deactivate_sql"], ("e6-chk", V1))
        elif change == "other_fingerprint":
            cur.execute(SQL["activate_sql"], ("e6-chk", V1, OTHER_FP, json.dumps([TOOL]), "admin"))
        elif change == "revoke_launch":
            cur.execute("UPDATE launch SET revoked_at = NOW() WHERE id = %s", (launch.id,))
        else:
            cur.execute("DELETE FROM role_grant WHERE grant_kind = 'operation'")
    assert _verify(token) is None


def test_the_activation_statements_match_the_schema(conn):
    with conn.cursor() as cur:
        cur.execute(SQL["activate_sql"], ("e6-chk", V1, FP, json.dumps(["a"]), "admin"))
        cur.execute(SQL["activate_sql"], ("e6-chk", V1, OTHER_FP, json.dumps(["b"]), "ops"))  # the upsert
        cur.execute(SQL["activation_sql"], ("e6-chk", V1))
        rows = cur.fetchall()
        assert len(rows) == 1 and rows[0]["manifest_fingerprint"] == OTHER_FP
        assert json.loads(rows[0]["allowed_actions"]) == ["b"]
        # The catalogue asks for exact (code, current) pairs: another activated version is not read.
        cur.execute(SQL["activate_sql"], ("e6-chk", "v-20260102-000000-bbbb", FP, json.dumps([]), "admin"))
        cur.execute(SQL["catalogue_sql"], ("e6-chk", V1, "e6-none", V1))
        assert [(r["artifact_code"], r["version_id"]) for r in cur.fetchall()] == [("e6-chk", V1)]
        assert cur.execute(SQL["deactivate_sql"], ("e6-chk", V1)) == 1
