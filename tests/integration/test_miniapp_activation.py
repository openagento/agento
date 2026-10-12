"""Miniapp approval per (artifact, agent_view), against the real schema (plan D-MINIAPP-2).

Only the toolbox is a stub: `admin_request` answers from an in-memory store, so the real
statements, the real retention lock and the real `launch` rows run.
"""
from __future__ import annotations

import json

import pytest

from agento.framework.access import accounts, launches, sessions
from agento.framework.event_manager import EventManager
from agento.modules.miniapps.src import routes as handlers
from agento.modules.versioned_artifacts.src import admin as va_admin
from agento.web import api, security, toolbox_client

from .conftest import _test_connection, routed

routes = routed("miniapps", handlers)

CODE = "ma-app"
V1, V2 = "v-20260101-000000-aaaa", "v-20260102-000000-bbbb"
C1, C2 = "1" * 40, "2" * 40
MANIFEST = {"title": "Notes", "actions": ["jira_search", "jira_add_comment"]}


@pytest.fixture
def conn():
    c = _test_connection(autocommit=True)
    _clean(c)
    yield c
    _clean(c)
    c.close()


def _clean(c):
    with c.cursor() as cur:
        cur.execute("DELETE FROM miniapp_activation WHERE artifact_code LIKE 'ma-%'")
        cur.execute("DELETE FROM versioned_artifact_audit WHERE artifact_code LIKE 'ma-%'")
        for table in ("launch", "session", "role_grant", "`user`"):
            cur.execute(f"DELETE FROM {table}")


@pytest.fixture
def world(conn):
    ids = {}
    with conn.cursor() as cur:
        cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES ('ma-ws', 'ma')")
        cur.execute("SELECT id FROM workspace WHERE code = 'ma-ws'")
        ids["ws"] = cur.fetchone()["id"]
        for key in ("view", "view2"):
            cur.execute("INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, %s, 'v')",
                        (ids["ws"], f"ma-{key}"))
            cur.execute("SELECT id FROM agent_view WHERE code = %s", (f"ma-{key}",))
            ids[key] = cur.fetchone()["id"]
    ids["admin"] = accounts.create_user(conn, "ma-admin", "admin", "correct horse battery")
    ids["user"] = accounts.create_user(conn, "ma-user", "user", "correct horse battery")
    for role in ("admin", "user"):
        accounts.add_grant(conn, role, "operation", "artifact.launch", agent_view_id=ids["view"])
    accounts.add_grant(conn, "user", "operation", "miniapps")
    return ids


class Store:
    """The toolbox /admin routes over one in-memory store."""

    def __init__(self, conn):
        self.conn = conn
        self.artifacts = {CODE: {"current": V1, "versions": {V1: C1}, "manifest": MANIFEST, "title": "Notes app"}}
        self.calls: list[tuple] = []
        self.locked_during_call: list[bool] = []
        self.status = 200
        self.during_call = None  # run while the caller holds what it holds (a lock)
        self.crash_after_delete = False  # the store is gone, the caller never hears back

    def __call__(self, conn, method, path, *, subject_id, body=None, timeout=30.0):
        self.calls.append((method, path, subject_id, body))
        other = _test_connection(autocommit=True)
        try:
            with other.cursor() as cur:
                cur.execute("SELECT IS_USED_LOCK(%s) AS owner", (launches.retention_lock_name(CODE),))
                self.locked_during_call.append(cur.fetchone()["owner"] is not None)
        finally:
            other.close()
        if self.during_call:
            self.during_call()
        if self.status != 200:
            return toolbox_client.InvokeResult(self.status, {"error_code": "X"})
        if method == "DELETE":
            self.artifacts.pop(path.split("/")[1])
            if self.crash_after_delete:
                raise RuntimeError("web died after the toolbox delete")
            return toolbox_client.InvokeResult(200, {})
        if method == "GET" and path == "versioned-artifacts":
            return toolbox_client.InvokeResult(200, {"artifacts": [
                {"artifact_code": c, "title": a["title"], "current_version": a["current"]}
                for c, a in self.artifacts.items()]})
        code = path.split("/")[1]
        art = self.artifacts.get(code)
        version = body["version"] if art is None or body["version"] != "current" else art["current"]
        if art is None or version not in art["versions"]:
            return toolbox_client.InvokeResult(404, {"error_code": "VERSION_NOT_FOUND"})
        return toolbox_client.InvokeResult(200, {"version_id": version, "content_id": art["versions"][version],
                                                 "manifest": art["manifest"]})


@pytest.fixture
def store(conn, monkeypatch):
    s = Store(conn)
    monkeypatch.setattr(toolbox_client, "admin_request", s)
    return s


def _req(conn, user, *, params=None, body=None):
    return api.Request(method="POST", path="/", headers={}, body=b"", cookies={},
                       origins=security.Origins(panel="https://panel.test", apps="https://apps.test"),
                       params={k: str(v) for k, v in (params or {}).items()}, conn=conn,
                       session=sessions.Session(id="sid", user=user, expires_at=None), json=body)


def _approve(conn, world, user=None, view=None, **body):
    body.setdefault("expected_version_id", V1)
    return routes.approve(_req(conn, user or world["admin"], params={"code": CODE, "view": view or world["view"]},
                               body=body))


def _launch(conn, world, user=None, *, as_preview=False, **body):
    handler = routes.create_preview if as_preview else routes.create_launch
    return handler(_req(conn, user or world["user"],
                        body={"agent_view_id": world["view"], "artifact_code": CODE, **body}))


def _rows(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _activation(conn, view):
    rows = _rows(conn, "SELECT * FROM miniapp_activation WHERE artifact_code = %s AND agent_view_id = %s",
                 (CODE, view))
    return rows[0] if rows else None


def _pinned(conn, launch_id):
    row = _rows(conn, "SELECT version_id, allowed_actions FROM launch WHERE id = %s", (launch_id,))[0]
    return row["version_id"], json.loads(row["allowed_actions"])


# --- approve and revoke ------------------------------------------------------------

def test_approve_pins_current_with_its_content_and_audits_it(conn, world, store):
    r = _approve(conn, world)
    assert r.status == 200
    row = _activation(conn, world["view"])
    assert (row["approved_version_id"], row["approved_content_id"]) == (V1, C1)
    assert json.loads(row["allowed_actions"]) == MANIFEST["actions"]  # default: every manifest action
    actor = f"service:web:user:{world['admin'].id}"
    assert row["activated_by"] == actor
    assert store.calls == [("POST", f"miniapps/{CODE}/resolve", actor, {"version": "current"})]
    assert store.locked_during_call == [True]  # resolve and the write run under the retention lock
    audit = _rows(conn, "SELECT operation, version_id, agent_view_id, actor FROM versioned_artifact_audit"
                        " WHERE artifact_code = %s", (CODE,))
    assert audit == [{"operation": "miniapp.approve", "version_id": V1, "agent_view_id": world["view"],
                      "actor": actor}]


def test_approve_is_a_compare_and_set_on_current(conn, world, store):
    r = _approve(conn, world, expected_version_id=V2)
    assert (r.status, r.body["current_version"]) == (409, V1)
    assert _activation(conn, world["view"]) is None


def test_approve_takes_a_subset_of_the_manifest_actions_only(conn, world, store):
    assert _approve(conn, world, actions=["jira_bash"]).status == 400
    assert _approve(conn, world, actions=["jira_search", "jira_search"]).status == 200
    assert json.loads(_activation(conn, world["view"])["allowed_actions"]) == ["jira_search"]


def test_approve_refuses_a_version_without_a_valid_manifest(conn, world, store):
    store.artifacts[CODE]["manifest"] = None
    assert _approve(conn, world).status == 409
    assert _activation(conn, world["view"]) is None


def test_approve_needs_the_leaf_and_the_launch_right_in_the_view(conn, world, store):
    assert _approve(conn, world, user=world["user"]).status == 403
    assert _approve(conn, world, view=world["view2"]).status == 404  # admin has no artifact.launch there
    assert store.calls == []


def test_approve_maps_toolbox_answers(conn, world, store):
    store.artifacts.clear()
    assert _approve(conn, world).status == 404
    store.status = 503
    assert _approve(conn, world).status == 503


def test_revoke_deletes_the_row_and_audits_it(conn, world, store):
    _approve(conn, world)
    req = _req(conn, world["admin"], params={"code": CODE, "view": world["view"]})
    assert routes.revoke(req).status == 204
    assert _activation(conn, world["view"]) is None
    assert routes.revoke(req).status == 404
    ops = [r["operation"] for r in _rows(conn, "SELECT operation FROM versioned_artifact_audit"
                                               " WHERE artifact_code = %s ORDER BY id", (CODE,))]
    assert ops == ["miniapp.approve", "miniapp.revoke"]


# --- launch --------------------------------------------------------------------------

def test_a_launch_needs_an_approval_in_the_view(conn, world, store):
    assert _launch(conn, world).status == 404
    assert store.calls == []


def test_a_launch_serves_the_approved_version_after_a_new_one_is_published(conn, world, store):
    """P1: the agent publishes a new version; the app stays listed and serves what was approved."""
    _approve(conn, world, actions=["jira_search"])
    store.artifacts[CODE]["versions"][V2] = C2
    store.artifacts[CODE]["current"] = V2
    r = _launch(conn, world)
    assert r.status == 201
    assert r.body["redeem"]["url"] == "https://apps.test/launch"
    assert _pinned(conn, r.body["launch_id"]) == (V1, ["jira_search"])
    assert store.calls[-1][3] == {"version": V1}  # resolve restores the approved version's tree
    assert store.locked_during_call[-1] is True
    rows = routes.catalogue(_req(conn, world["user"], params={"id": world["view"]})).body
    assert rows == [{"artifact_code": CODE, "version_id": V1, "title": "Notes app", "current_version": V2,
                     "update_waiting": True}]


@pytest.mark.parametrize("gone", ["other_content", "missing_version"])
def test_a_reused_version_id_with_other_content_is_refused_and_the_row_removed(conn, world, store, gone):
    """The pin is the content: a re-created artifact that reuses the version id with other
    bytes does not inherit the approval."""
    _approve(conn, world)
    if gone == "other_content":
        store.artifacts[CODE]["versions"][V1] = C2
    else:
        store.artifacts[CODE]["versions"].clear()
    r = _launch(conn, world)
    assert (r.status, r.body) == (409, {"error": "approved version gone"})
    assert _activation(conn, world["view"]) is None
    assert not _rows(conn, "SELECT id FROM launch")


def test_a_reused_version_id_with_the_same_content_launches(conn, world, store):
    _approve(conn, world)
    store.artifacts[CODE] = {**store.artifacts[CODE], "versions": {V1: C1}}  # re-created, same bytes
    assert _launch(conn, world).status == 201


def test_a_toolbox_failure_at_launch_keeps_the_approval(conn, world, store):
    _approve(conn, world)
    store.status = 503
    assert _launch(conn, world).status == 503
    assert _activation(conn, world["view"]) is not None


def test_preview_launches_current_with_no_actions_even_over_an_older_approval(conn, world, store):
    _approve(conn, world)
    store.artifacts[CODE]["versions"][V2] = C2
    store.artifacts[CODE]["current"] = V2
    r = _launch(conn, world, user=world["admin"], as_preview=True)
    assert r.status == 201
    assert _pinned(conn, r.body["launch_id"]) == (V2, [])
    assert store.calls[-1][3] == {"version": "current"}


def test_preview_needs_the_approve_leaf(conn, world, store):
    assert _launch(conn, world, as_preview=True).status == 403
    assert store.calls == []
    accounts.add_grant(conn, "user", "operation", "miniapps.approve")
    assert _launch(conn, world, as_preview=True).status == 201


def test_preview_of_an_artifact_with_no_current_is_409(conn, world, store, monkeypatch):
    def no_current(conn, method, path, **kw):
        return toolbox_client.InvokeResult(400, {"error_code": "INVALID_PATH"})

    monkeypatch.setattr(toolbox_client, "admin_request", no_current)
    assert _launch(conn, world, user=world["admin"], as_preview=True).status == 409


@pytest.mark.parametrize(("body", "status"), [
    ({"artifact_code": "Bad!"}, 400),
    ({"agent_view_id": None}, 400),
    ({"workspace_id": 1}, 400),
    ({"preview": "yes"}, 400),
    ({"preview": True}, 400),  # a preview is its own route: POST /api/miniapps/previews
])
def test_launch_input_errors(conn, world, store, body, status):
    assert _launch(conn, world, **body).status == status


def test_launch_without_the_launch_right_is_404(conn, world, store):
    _approve(conn, world)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM role_grant WHERE role = 'user'")
    assert _launch(conn, world).status == 404


# --- catalogue -----------------------------------------------------------------------

def test_the_catalogue_lists_this_views_approvals_known_to_the_store(conn, world, store):
    _approve(conn, world)
    with conn.cursor() as cur:  # an approval whose artifact left the store
        cur.execute("INSERT INTO miniapp_activation (artifact_code, agent_view_id, approved_version_id,"
                    " approved_content_id, allowed_actions, activated_by) VALUES ('ma-gone', %s, %s, %s, '[]', 'x')",
                    (world["view"], V1, C1))
    rows = routes.catalogue(_req(conn, world["user"], params={"id": world["view"]})).body
    assert rows == [{"artifact_code": CODE, "version_id": V1, "title": "Notes app", "current_version": V1,
                     "update_waiting": False}]
    assert routes.catalogue(_req(conn, world["admin"], params={"id": world["view2"]})).body == []


def test_the_catalogue_asks_the_toolbox_nothing_when_nothing_is_approved(conn, world, store):
    assert routes.catalogue(_req(conn, world["user"], params={"id": world["view"]})).body == []
    assert store.calls == []


def test_the_catalogue_needs_the_leaf_a_reachable_view_and_lists_nothing_without_the_launch_right(
        conn, world, store):
    _approve(conn, world, view=None)
    view2 = _req(conn, world["user"], params={"id": world["view2"]})
    assert routes.catalogue(view2).status == 404  # not reachable: no disclosure
    with conn.cursor() as cur:  # reachable through another grant, but no artifact.launch there
        cur.execute("INSERT INTO role_grant (role, grant_kind, name, agent_view_id) VALUES ('user', 'tool', 'x', %s)",
                    (world["view2"],))
    assert (routes.catalogue(view2).status, routes.catalogue(view2).body) == (200, [])
    with conn.cursor() as cur:
        cur.execute("DELETE FROM role_grant WHERE role = 'user' AND name = 'miniapps'")
    assert routes.catalogue(_req(conn, world["user"], params={"id": world["view"]})).status == 403


def test_a_toolbox_failure_in_the_catalogue_is_503(conn, world, store):
    _approve(conn, world)
    store.status = 503
    assert routes.catalogue(_req(conn, world["user"], params={"id": world["view"]})).status == 503


# --- panel reads: the views to pick from, the approval page ---------------------------

def test_the_launch_views_list_only_views_with_the_launch_right_and_need_the_leaf(conn, world, store):
    with conn.cursor() as cur:  # view2 reachable through another grant, but no artifact.launch there
        cur.execute("INSERT INTO role_grant (role, grant_kind, name, agent_view_id) VALUES ('user', 'tool', 'x', %s)",
                    (world["view2"],))
    r = routes.launch_views(_req(conn, world["user"]))
    assert (r.status, [v["id"] for v in r.body]) == (200, [world["view"]])
    assert set(r.body[0]) == {"id", "code", "label", "workspace_id"}
    with conn.cursor() as cur:
        cur.execute("DELETE FROM role_grant WHERE role = 'user' AND name = 'miniapps'")
    assert routes.launch_views(_req(conn, world["user"])).status == 403  # the panel probe: module hidden
    assert store.calls == []


def test_the_launch_views_open_to_an_approver_without_the_catalogue_leaf(conn, world, store):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM role_grant WHERE role = 'user' AND name = 'miniapps'")
    accounts.add_grant(conn, "user", "operation", "miniapps.approve")
    r = routes.launch_views(_req(conn, world["user"]))
    assert (r.status, [v["id"] for v in r.body]) == (200, [world["view"]])
    assert routes.catalogue(_req(conn, world["user"], params={"id": world["view"]})).status == 403


def test_the_launch_views_take_one_query_for_any_number_of_views(conn, world, store, monkeypatch):
    accounts.add_grant(conn, "user", "operation", "artifact.launch", workspace_id=world["ws"])  # both views
    queries = []
    real = type(conn).cursor

    def counting(self, *a, **k):
        cur = real(self, *a, **k)
        execute = cur.execute
        cur.execute = lambda sql, *args: (queries.append(sql), execute(sql, *args))[1]
        return cur
    monkeypatch.setattr(type(conn), "cursor", counting)
    views = accounts.operation_views(conn, "user", "artifact.launch")
    assert {v["id"] for v in views} >= {world["view"], world["view2"]}
    assert len(queries) == 1


def test_the_approval_page_shows_the_approval_and_current_with_its_manifest(conn, world, store):
    req = _req(conn, world["admin"], params={"code": CODE, "view": world["view"]})
    r = routes.approval(req)
    assert (r.status, r.body["approval"]) == (200, None)
    assert r.body["current"] == {"version_id": V1, "title": "Notes", "actions": MANIFEST["actions"]}
    _approve(conn, world, actions=["jira_search"])
    store.artifacts[CODE].update(current=V2, versions={V1: C1, V2: C2})
    r = routes.approval(req)
    assert r.body["current"]["version_id"] == V2
    a = r.body["approval"]
    assert (a["approved_version_id"], a["allowed_actions"]) == (V1, ["jira_search"])
    assert a["activated_by"] == f"service:web:user:{world['admin'].id}" and a["activated_at"]


def test_the_approval_page_shows_an_invalid_manifest_as_no_actions(conn, world, store):
    store.artifacts[CODE]["manifest"] = None
    r = routes.approval(_req(conn, world["admin"], params={"code": CODE, "view": world["view"]}))
    assert r.body["current"] == {"version_id": V1, "title": None, "actions": None}


def test_the_approval_page_has_the_approve_guards(conn, world, store):
    assert routes.approval(_req(conn, world["user"], params={"code": CODE, "view": world["view"]})).status == 403
    assert routes.approval(_req(conn, world["admin"], params={"code": CODE, "view": world["view2"]})).status == 404
    store.artifacts.clear()
    assert routes.approval(_req(conn, world["admin"], params={"code": CODE, "view": world["view"]})).status == 404


def test_the_approvals_list_every_view_the_user_may_launch_in(conn, world, store):
    _approve(conn, world, actions=["jira_search"])
    with conn.cursor() as cur:  # view2: no artifact.launch there, so not listed
        cur.execute("INSERT INTO miniapp_activation (artifact_code, agent_view_id, approved_version_id,"
                    " approved_content_id, allowed_actions, activated_by) VALUES (%s, %s, %s, %s, '[]', 'x')",
                    (CODE, world["view2"], V1, C1))
    r = routes.approvals(_req(conn, world["admin"], params={"code": CODE}))
    assert r.status == 200
    rows = json.loads(json.dumps(r.body))  # the server json.dumps the body
    assert [(a["agent_view_id"], a["approved_version_id"], a["allowed_actions"]) for a in rows] == [
        (world["view"], V1, ["jira_search"])]
    assert rows[0]["activated_by"] == f"service:web:user:{world['admin'].id}"
    assert rows[0]["activated_at"].endswith("Z")
    assert routes.approvals(_req(conn, world["user"], params={"code": CODE})).status == 403
    assert routes.approvals(_req(conn, world["admin"], params={"code": "-bad"})).status == 404


# --- delete (plan §2.4: a stale approval cannot be inherited) -------------------------

@pytest.fixture
def no_observers(monkeypatch):
    """`miniapps` disabled: nothing observes `artifact_delete_after`."""
    monkeypatch.setattr("agento.framework.event_manager._EVENT_MANAGER", EventManager())


def _recreate_with_other_bytes(store):
    """Deleted and made again: the same version id (forced), other content."""
    store.artifacts[CODE] = {"current": V1, "versions": {V1: C2}, "manifest": MANIFEST, "title": "Notes app"}


def _redeemed_token(conn, world) -> str:
    r = _launch(conn, world)
    assert r.status == 201
    redeemed = launches.redeem(conn, r.body["launch_id"], r.body["redeem"]["fields"]["code"])
    assert redeemed is not None
    return redeemed[1]


def test_an_approval_left_by_a_delete_with_miniapps_disabled_is_refused_at_launch(conn, world, store, no_observers):
    _approve(conn, world)
    va_admin.delete_artifact(conn, world["admin"], CODE)
    assert _activation(conn, world["view"]) is not None  # the observer did not run
    _recreate_with_other_bytes(store)  # miniapps enabled again
    r = _launch(conn, world)
    assert (r.status, r.body) == (409, {"error": "approved version gone"})
    assert _activation(conn, world["view"]) is None
    assert not _rows(conn, "SELECT id FROM launch WHERE revoked_at IS NULL")


def test_a_crash_after_the_store_removal_leaves_no_way_to_the_replacement_files(conn, world, store, no_observers):
    _approve(conn, world)
    token = _redeemed_token(conn, world)
    assert launches.authorize_files(conn, [token], CODE, V1)
    store.crash_after_delete = True
    with pytest.raises(RuntimeError):
        va_admin.delete_artifact(conn, world["admin"], CODE)
    assert CODE not in store.artifacts and _activation(conn, world["view"]) is not None
    _recreate_with_other_bytes(store)
    assert not launches.authorize_files(conn, [token], CODE, V1)  # revoked before the store went
    assert _launch(conn, world).status == 409
    assert _activation(conn, world["view"]) is None


def test_a_delete_cannot_run_between_a_launchs_resolve_and_its_commit(conn, world, store, no_observers, monkeypatch):
    monkeypatch.setattr(launches, "RETENTION_LOCK_WAIT_SECONDS", 0)
    _approve(conn, world)
    refused = []

    def delete_now():
        store.during_call = None
        other = _test_connection(autocommit=True)
        try:
            va_admin.delete_artifact(other, world["admin"], CODE)
        except launches.RetentionBusy:
            refused.append(True)
        finally:
            other.close()

    store.during_call = delete_now  # the launch has resolved and holds the lock until its commit
    r = _launch(conn, world)
    assert (r.status, refused) == (201, [True])
    assert CODE in store.artifacts
    va_admin.delete_artifact(conn, world["admin"], CODE)  # after the commit: it revokes that launch
    assert _rows(conn, "SELECT revoked_at FROM launch WHERE id = %s", (r.body["launch_id"],))[0]["revoked_at"]


def test_a_launch_cannot_commit_while_a_delete_runs(conn, world, store, no_observers, monkeypatch):
    monkeypatch.setattr(launches, "RETENTION_LOCK_WAIT_SECONDS", 0)
    _approve(conn, world)
    answers = []

    def launch_now():
        store.during_call = None
        other = _test_connection(autocommit=True)
        try:
            answers.append(_launch(other, world).status)
        finally:
            other.close()

    store.during_call = launch_now  # the delete holds the lock: launches revoked, store removal next
    va_admin.delete_artifact(conn, world["admin"], CODE)
    assert answers == [503]  # "artifact busy, try again"
    assert not _rows(conn, "SELECT id FROM launch")
