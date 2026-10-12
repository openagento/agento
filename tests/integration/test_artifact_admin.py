"""Artifact administration from the panel (plan D-MINIAPP-2 §2.4), against the real schema.

Only the toolbox is a stub: `admin_request` answers from an in-memory store, so the real
statements, the real retention lock, the real `launch` rows and the real miniapps observer run.
"""
from __future__ import annotations

import json

import pytest

from agento.framework.access import accounts, launches, sessions
from agento.framework.database_config import DatabaseConfig
from agento.framework.event_manager import EventManager, ObserverEntry
from agento.modules.miniapps.src.observers import DropActivationsOnArtifactDelete
from agento.modules.versioned_artifacts.src import routes as handlers
from agento.web import api, security, toolbox_client

from .conftest import _test_connection, routed

routes = routed("versioned_artifacts", handlers)

CODE = "aa-site"
V1, V2 = "v-20260101-000000-aaaa", "v-20260102-000000-bbbb"


@pytest.fixture
def conn():
    c = _test_connection(autocommit=True)
    _clean(c)
    yield c
    _clean(c)
    c.close()


def _clean(c):
    with c.cursor() as cur:
        for table in ("miniapp_activation", "versioned_artifact_audit", "versioned_artifact"):
            cur.execute(f"DELETE FROM {table} WHERE artifact_code LIKE 'aa-%'")
        for table in ("launch", "session", "role_grant", "`user`"):
            cur.execute(f"DELETE FROM {table}")


@pytest.fixture
def world(conn):
    ids = {}
    with conn.cursor() as cur:
        cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES ('aa-ws', 'aa')")
        cur.execute("SELECT id FROM workspace WHERE code = 'aa-ws'")
        ids["ws"] = cur.fetchone()["id"]
        cur.execute("INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, 'aa-view', 'v')",
                    (ids["ws"],))
        cur.execute("SELECT id FROM agent_view WHERE code = 'aa-view'")
        ids["view"] = cur.fetchone()["id"]
    ids["admin"] = accounts.create_user(conn, "aa-admin", "admin", "correct horse battery")
    ids["user"] = accounts.create_user(conn, "aa-user", "user", "correct horse battery")
    accounts.add_grant(conn, "admin", "operation", "artifact.launch", agent_view_id=ids["view"])
    return ids


class Store:
    """The toolbox /admin/versioned-artifacts routes over one in-memory store."""

    def __init__(self):
        self.artifacts = {CODE: {"current": V1, "versions": [V2, V1]}}
        self.calls: list[tuple] = []
        self.locked_during_call: list[bool] = []
        self.status: int | None = None
        self.stale = False

    def __call__(self, conn, method, path, *, subject_id, body=None, timeout=30.0):
        self.calls.append((method, path, subject_id, body))
        other = _test_connection(autocommit=True)
        try:
            with other.cursor() as cur:
                cur.execute("SELECT IS_USED_LOCK(%s) AS owner", (launches.retention_lock_name(CODE),))
                self.locked_during_call.append(cur.fetchone()["owner"] is not None)
                cur.execute("SELECT COUNT(*) AS n FROM launch WHERE artifact_code = %s AND revoked_at IS NULL",
                            (CODE,))
                self.live_launches = cur.fetchone()["n"]
        finally:
            other.close()
        if self.status is not None:
            return toolbox_client.InvokeResult(self.status, {"error_code": "X", "message": "m"})
        parts = path.split("/")[1:]
        if not parts:
            return toolbox_client.InvokeResult(200, {"artifacts": [
                {"artifact_code": c, "title": "Site", "current_version": a["current"]}
                for c, a in self.artifacts.items()]})
        art = self.artifacts.get(parts[0])
        if art is None:
            return toolbox_client.InvokeResult(404, {"error_code": "ARTIFACT_NOT_FOUND", "message": "gone"})
        if method == "DELETE":
            del self.artifacts[parts[0]]
            return toolbox_client.InvokeResult(200, {"artifact_code": parts[0]})
        if parts[1] == "versions":
            return toolbox_client.InvokeResult(200, {"versions": [
                {"version_id": v, "revision": "f" * 40, "preview_path": f"{parts[0]}/v/{v}/"}
                for v in art["versions"]]})
        if body["expected_current_version"] != art["current"]:
            return toolbox_client.InvokeResult(409, {"error_code": "CURRENT_VERSION_CHANGED", "message": "moved",
                                                     "current_version": art["current"]})
        art["current"] = body["version_id"]
        return toolbox_client.InvokeResult(200, {"artifact_code": parts[0], "current_version": body["version_id"],
                                                 **({"preview_stale": True} if self.stale else {})})


@pytest.fixture
def store(monkeypatch):
    s = Store()
    monkeypatch.setattr(toolbox_client, "admin_request", s)
    return s


@pytest.fixture
def observers(monkeypatch, int_db_config):
    """The miniapps observer, registered the way `web` registers it, on the test database."""
    em = EventManager()
    em.register("artifact_delete_after", ObserverEntry("miniapps_drop_activations", DropActivationsOnArtifactDelete))
    monkeypatch.setattr("agento.framework.event_manager._EVENT_MANAGER", em)
    monkeypatch.setattr(DatabaseConfig, "from_env", classmethod(lambda cls: int_db_config))
    return em


def _req(conn, user, *, params=None, body=None):
    return api.Request(method="POST", path="/", headers={}, body=b"", cookies={},
                       origins=security.Origins(panel="https://panel.test", apps="https://apps.test"),
                       params={k: str(v) for k, v in (params or {}).items()}, conn=conn,
                       session=sessions.Session(id="sid", user=user, expires_at=None), json=body)


def _rows(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _subject(user):
    return f"service:web:user:{user.id}"


def _launch(conn, world) -> str:
    launch, _ = launches.create_launch(conn, world["admin"], workspace_id=world["ws"], agent_view_id=world["view"],
                                       artifact_code=CODE, version_id=V1, allowed_actions=[])
    with conn.cursor() as cur:
        cur.execute("UPDATE launch SET exchange_redeemed_at = NOW() WHERE id = %s", (launch.id,))
    return launch.id


def _approval(conn, world):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO miniapp_activation (artifact_code, agent_view_id, approved_version_id,"
                    " approved_content_id, allowed_actions, activated_by) VALUES (%s, %s, %s, %s, '[]', 'x')",
                    (CODE, world["view"], V1, "1" * 40))


# --- leaves ------------------------------------------------------------------------------

@pytest.mark.parametrize(("handler", "params", "body"), [
    ("list_artifacts", {}, None),
    ("versions", {"code": CODE}, None),
    ("make_current", {"code": CODE}, {"version_id": V2, "expected_current_version": V1}),
    ("delete", {"code": CODE}, None),
])
def test_every_route_needs_its_leaf(conn, world, store, handler, params, body):
    r = getattr(routes, handler)(_req(conn, world["user"], params=params, body=body))
    assert r.status == 403
    assert store.calls == []


@pytest.mark.parametrize(("leaf", "handler", "allowed"), [
    ("versioned_artifacts", "make_current", False),
    ("versioned_artifacts.publish", "make_current", True),
    ("versioned_artifacts.delete", "delete", True),
    ("versioned_artifacts", "delete", False),
])
def test_a_role_with_one_leaf_reaches_only_that_route(conn, world, store, leaf, handler, allowed):
    accounts.add_grant(conn, "user", "operation", leaf)
    body = {"version_id": V2, "expected_current_version": V1}
    r = getattr(routes, handler)(_req(conn, world["user"], params={"code": CODE}, body=body))
    assert (r.status != 403) is allowed


# --- list and history --------------------------------------------------------------------

def test_the_list_is_the_store_list(conn, world, store):
    r = routes.list_artifacts(_req(conn, world["admin"]))
    assert (r.status, r.body) == (200, [{"artifact_code": CODE, "title": "Site", "current_version": V1}])
    assert store.calls == [("GET", "versioned-artifacts", _subject(world["admin"]), None)]


def test_the_history_joins_each_version_with_its_audit_rows_in_one_query(conn, world, store):
    with conn.cursor() as cur:
        for op, version, job, actor, description in [
            ("versioned_artifact.version.saved", V1, 790, "agent", "First version"),
            ("versioned_artifact.version.published", V1, 790, "agent", None),
            ("versioned_artifact.version.saved", V2, 812, "agent", "Adds CSV export"),
            ("versioned_artifact.version.saved", "v-20250101-000000-cccc", 1, "agent", "not listed"),
        ]:
            cur.execute("INSERT INTO versioned_artifact_audit (artifact_code, operation, version_id, job_id,"
                        " agent_view_id, actor, result, description) VALUES (%s, %s, %s, %s, %s, %s, 'ok', %s)",
                        (CODE, op, version, job, world["view"], actor, description))
        cur.execute("INSERT INTO versioned_artifact_audit (artifact_code, operation, version_id, result)"
                    " VALUES (%s, 'versioned_artifact.version.published', %s, 'error')", (CODE, V2))
    r = routes.versions(_req(conn, world["admin"], params={"code": CODE}))
    assert r.status == 200
    assert [v["version_id"] for v in r.body] == [V2, V1]
    assert "revision" not in r.body[0]  # Git stays internal
    assert r.body[0]["preview_path"] == f"{CODE}/v/{V2}/"
    assert [(e["operation"], e["job_id"], e["description"]) for e in r.body[0]["events"]] == [
        ("versioned_artifact.version.saved", 812, "Adds CSV export")]
    assert [e["operation"] for e in r.body[1]["events"]] == [
        "versioned_artifact.version.saved", "versioned_artifact.version.published"]
    assert r.body[1]["events"][0]["agent_view_id"] == world["view"]
    # The server json.dumps the body: a DB DATETIME must already be an ISO string.
    assert json.loads(json.dumps(r.body))[0]["events"][0]["created_at"].endswith("Z")


def test_the_history_of_an_unknown_artifact_is_404(conn, world, store):
    assert routes.versions(_req(conn, world["admin"], params={"code": "aa-nope"})).status == 404


# --- make current ------------------------------------------------------------------------

def test_make_current_is_a_compare_and_set(conn, world, store):
    ok = routes.make_current(_req(conn, world["admin"], params={"code": CODE},
                                  body={"version_id": V2, "expected_current_version": V1}))
    assert (ok.status, ok.body["current_version"]) == (200, V2)
    assert store.calls[-1] == ("POST", f"versioned-artifacts/{CODE}/publish", _subject(world["admin"]),
                               {"version_id": V2, "expected_current_version": V1})
    stale = routes.make_current(_req(conn, world["admin"], params={"code": CODE},
                                     body={"version_id": V1, "expected_current_version": V1}))
    assert (stale.status, stale.body["current_version"]) == (409, V2)


def test_make_current_passes_preview_stale_through(conn, world, store):
    store.stale = True
    r = routes.make_current(_req(conn, world["admin"], params={"code": CODE},
                                 body={"version_id": V2, "expected_current_version": V1}))
    assert (r.status, r.body["current_version"], r.body["preview_stale"]) == (200, V2, True)


@pytest.mark.parametrize("body", [{}, {"version_id": V2}, {"version_id": "nope", "expected_current_version": V1}])
def test_make_current_input_errors(conn, world, store, body):
    assert routes.make_current(_req(conn, world["admin"], params={"code": CODE}, body=body)).status == 400
    assert store.calls == []


def test_a_toolbox_failure_is_503(conn, world, store):
    store.status = 500
    assert routes.list_artifacts(_req(conn, world["admin"])).status == 503


# --- delete ------------------------------------------------------------------------------

def test_delete_revokes_launches_first_then_removes_store_row_and_approvals(conn, world, store, observers):
    launch_id = _launch(conn, world)
    _approval(conn, world)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO versioned_artifact (artifact_code, title) VALUES (%s, 'Site')", (CODE,))
    r = routes.delete(_req(conn, world["admin"], params={"code": CODE}))
    assert r.status == 204
    assert store.calls == [("DELETE", f"versioned-artifacts/{CODE}", _subject(world["admin"]), None)]
    assert store.locked_during_call == [True]
    assert store.live_launches == 0  # revoked and committed before the store goes
    assert _rows(conn, "SELECT revoked_at FROM launch WHERE id = %s", (launch_id,))[0]["revoked_at"] is not None
    assert not _rows(conn, "SELECT 1 FROM versioned_artifact WHERE artifact_code = %s", (CODE,))
    assert not _rows(conn, "SELECT 1 FROM miniapp_activation WHERE artifact_code = %s", (CODE,))


def test_a_retry_after_the_store_is_gone_finishes_the_cleanup(conn, world, store, observers):
    _approval(conn, world)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO versioned_artifact (artifact_code, title) VALUES (%s, 'Site')", (CODE,))
    store.artifacts.clear()  # the toolbox delete ran, the web cleanup did not
    assert routes.delete(_req(conn, world["admin"], params={"code": CODE})).status == 204
    assert not _rows(conn, "SELECT 1 FROM versioned_artifact WHERE artifact_code = %s", (CODE,))
    assert not _rows(conn, "SELECT 1 FROM miniapp_activation WHERE artifact_code = %s", (CODE,))


def test_a_failed_toolbox_delete_keeps_the_row_and_the_approvals(conn, world, store, observers):
    _approval(conn, world)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO versioned_artifact (artifact_code, title) VALUES (%s, 'Site')", (CODE,))
    store.status = 500
    assert routes.delete(_req(conn, world["admin"], params={"code": CODE})).status == 503
    assert _rows(conn, "SELECT 1 FROM versioned_artifact WHERE artifact_code = %s", (CODE,))
    assert _rows(conn, "SELECT 1 FROM miniapp_activation WHERE artifact_code = %s", (CODE,))


def test_a_failed_revoke_aborts_the_delete(conn, world, store, observers, monkeypatch):
    def boom(conn, code):
        raise RuntimeError("db down")

    monkeypatch.setattr(launches, "revoke_for_artifact", boom)
    with pytest.raises(RuntimeError):  # server._run answers 500
        routes.delete(_req(conn, world["admin"], params={"code": CODE}))
    assert store.calls == []
    assert CODE in store.artifacts


def test_an_observer_failure_does_not_fail_the_delete(conn, world, store, observers, monkeypatch):
    monkeypatch.setattr(DatabaseConfig, "from_env", classmethod(lambda cls: DatabaseConfig(mysql_host="127.0.0.1", mysql_port=1)))
    assert routes.delete(_req(conn, world["admin"], params={"code": CODE})).status == 204


def test_revoke_for_artifact_ends_only_that_artifacts_live_launches(conn, world):
    mine = _launch(conn, world)
    other, _ = launches.create_launch(conn, world["admin"], workspace_id=world["ws"], agent_view_id=world["view"],
                                      artifact_code="aa-other", version_id=V1, allowed_actions=[])
    launches.revoke_for_artifact(conn, CODE)
    launches.revoke_for_artifact(conn, CODE)  # idempotent
    revoked = {r["id"]: r["revoked_at"] is not None for r in _rows(conn, "SELECT id, revoked_at FROM launch")}
    assert revoked == {mine: True, other.id: False}
