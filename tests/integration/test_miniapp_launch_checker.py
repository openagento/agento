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
from agento.modules.miniapps.src.activation import APPROVE_SQL
from agento.web.toolbox_client import invoke_launch_action

from .conftest import _test_connection

_ROOT = Path(__file__).resolve().parents[2]
_DRIVER = Path(__file__).parent / "node" / "verify_capability.mjs"
SQL = {k: v.replace("?", "%s") for k, v in
       json.loads((_ROOT / "tests" / "fixtures" / "miniapp_sql_v1.json").read_text()).items() if k.endswith("_sql")}
V1, V2 = "v-20260101-000000-aaaa", "v-20260102-000000-bbbb"
CONTENT = "c" * 40
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
        for code in ("e6-chk-view", "e6-chk-view2"):
            cur.execute("INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, %s, 'v')", (ws, code))
        cur.execute("SELECT id FROM agent_view WHERE code IN ('e6-chk-view', 'e6-chk-view2') ORDER BY code")
        view, view2 = (r["id"] for r in cur.fetchall())
    return ws, view, view2


def _approve(conn, view, version=V1):
    with conn.cursor() as cur:
        cur.execute(APPROVE_SQL, ("e6-chk", view, version, CONTENT, json.dumps([TOOL]), "service:web:user:1"))


@pytest.fixture
def launched(conn, scope):
    """alice may launch in the view; e6-chk V1 is approved there with TOOL; a redeemed launch of it.

    alice holds no tool grant: the approval is the grant (O-2)."""
    ws, view, _view2 = scope
    user = accounts.create_user(conn, "alice", "user", "correct horse battery")
    accounts.add_grant(conn, "user", "operation", "artifact.launch", agent_view_id=view)
    _approve(conn, view)
    launch, code = launches.create_launch(conn, user, workspace_id=ws, agent_view_id=view, artifact_code="e6-chk",
                                          version_id=V1, allowed_actions=[TOOL])
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
    """Its tools are the approval's actions, with no role tool grant (O-2)."""
    session, launch = launched
    verified = _verify(_mint(conn, session, launch))
    assert verified is not None
    ctx = verified["context"]
    assert (ctx["kind"], ctx["tool_ceiling"], verified["single_use"]) == ("miniapp", [TOOL], True)
    assert ctx["app"] == {"artifact_code": "e6-chk", "version_id": V1, "launch_id": launch.id}
    assert verified["permitted_tools"] == [TOOL]


@pytest.mark.parametrize("change", ["revoke_approval", "approve_other_version", "approve_other_view_only",
                                    "revoke_launch", "remove_operation"])
def test_the_toolbox_refuses_it_once_the_launch_or_approval_is_gone(conn, scope, launched, change):
    session, launch = launched
    token = _mint(conn, session, launch)
    with conn.cursor() as cur:
        if change == "revoke_approval":
            cur.execute("DELETE FROM miniapp_activation WHERE artifact_code = 'e6-chk'")
        elif change == "approve_other_version":
            _approve(conn, scope[1], V2)
        elif change == "approve_other_view_only":
            cur.execute("DELETE FROM miniapp_activation WHERE artifact_code = 'e6-chk'")
            _approve(conn, scope[2])
        elif change == "revoke_launch":
            cur.execute("UPDATE launch SET revoked_at = NOW() WHERE id = %s", (launch.id,))
        else:
            cur.execute("DELETE FROM role_grant WHERE grant_kind = 'operation'")
    assert _verify(token) is None


def test_the_approval_upsert_and_the_retention_pin_match_the_schema(conn, scope):
    _ws, view, view2 = scope
    _approve(conn, view)
    _approve(conn, view, V2)  # the upsert: one row per (artifact, view)
    _approve(conn, view2)
    with conn.cursor() as cur:
        cur.execute(SQL["retention_pin_sql"], ("e6-chk",))
        assert sorted(r["version_id"] for r in cur.fetchall()) == [V1, V2]
