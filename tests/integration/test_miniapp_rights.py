"""Per-app rights (D-MINIAPP-3) through the routes, against the real schema.

The world, the store stub and the helpers are the ones of test_miniapp_activation.py: ma-user
is a member (view, no flags) with ``artifact.launch`` and a grant for every manifest tool in
``view``; ma-none has no rights row.
"""
# ruff: noqa: F811
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
from agento.modules.miniapps.src import activation
from agento.web.toolbox_client import invoke_launch_action

from .test_miniapp_activation import (  # noqa: F401 (fixtures; tests take them as arguments, F811)
    CODE,
    V1,
    _approve,
    _launch,
    _req,
    _rows,
    conn,
    routes,
    store,
    world,
)

_ROOT = Path(__file__).resolve().parents[2]


def _job(conn, world, key: str, trust: str) -> None:
    """``key``/``trust`` ran the job whose ``init`` made the artifact."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO job (type, source, agent_view_id, reference_id, idempotency_key, status,"
                    " requester_key, requester_trust) VALUES ('todo', 'panel', %s, 'ma', %s, 'SUCCESS', %s, %s)",
                    (world["view"], f"ma-{key}-{trust}", key, trust))
        cur.execute("UPDATE versioned_artifact SET created_job_id = %s WHERE artifact_code = %s", (cur.lastrowid, CODE))


@pytest.fixture(autouse=True)
def _jobs(conn):
    yield
    with conn.cursor() as cur:
        cur.execute("UPDATE versioned_artifact SET created_job_id = NULL WHERE artifact_code LIKE 'ma-%'")
        cur.execute("DELETE FROM job WHERE idempotency_key LIKE 'ma-%'")


def _member(conn, *, user=None, role=None, edit=False, remove=False):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM miniapp_member WHERE artifact_code = %s", (CODE,))
        cur.execute("INSERT INTO miniapp_member (artifact_code, user_id, role, can_edit, can_remove)"
                    " VALUES (%s, %s, %s, %s, %s)", (CODE, user.id if user else None, role, int(edit), int(remove)))


def _can(conn, user):
    r = routes.app(_req(conn, user, params={"code": CODE}))
    return r.body["can"] if r.status == 200 else r.status


# --- the rights matrix -----------------------------------------------------------------

def test_rights_matrix(conn, world, store):
    u, none = world["user"], world["none"]
    full = {"view": True, "edit": True, "remove": True}
    assert _can(conn, world["admin"]) == full
    assert _can(conn, u) == {"view": True, "edit": False, "remove": False}  # user member
    _member(conn, user=u, edit=True)
    assert _can(conn, u) == {"view": True, "edit": True, "remove": False}
    _member(conn, role="user", remove=True)  # role member: every user of the role
    assert _can(conn, u) == _can(conn, none) == {"view": True, "edit": False, "remove": True}
    _member(conn, role="admin")
    assert _can(conn, u) == 404  # no view: no disclosure
    _job(conn, world, f"user:{none.id}", "domain")  # a channel requester that looks like the user
    assert _can(conn, none) == 404
    _job(conn, world, f"user:{none.id}", "account")  # the panel user whose job made it
    assert _can(conn, none) == full


def test_members_are_shown_with_edit_only(conn, world, store):
    assert routes.app(_req(conn, world["user"], params={"code": CODE})).body["members"] is None
    members = routes.app(_req(conn, world["admin"], params={"code": CODE})).body["members"]
    assert [(m["username"], m["can_edit"], m["can_remove"]) for m in members] == [("ma-user", False, False)]


def test_a_role_member_with_no_view_reachable_still_gets_the_catalogue_row(conn, world, store):
    _approve(conn, world)
    _member(conn, role="user")
    with conn.cursor() as cur:  # the role reaches no view
        cur.execute("DELETE FROM role_grant WHERE role = 'user'")
    rows = routes.catalogue(_req(conn, world["none"])).body
    assert [(r["artifact_code"], r["views"], r["can"]["execute"]) for r in rows] == [(CODE, [], False)]


# --- writes ------------------------------------------------------------------------------

def _put(conn, user, **body):
    return routes.put_member(_req(conn, user, params={"code": CODE}, body=body)).status


def test_member_options_need_edit_and_list_active_users_and_roles(conn, world, store):
    get = lambda user: routes.member_options(_req(conn, user, params={"code": CODE}))  # noqa: E731
    assert get(world["user"]).status == 403  # a member without edit
    assert get(world["none"]).status == 404  # no view: no disclosure
    accounts.set_active(conn, world["none"].id, False)
    body = get(world["admin"]).body
    assert body["users"] == ["ma-admin", "ma-user"]  # the inactive ma-none is not offered
    assert {"code": "user", "label": "User"} in body["roles"]


def test_remove_cannot_be_escalated(conn, world, store):
    _member(conn, user=world["user"], edit=True)
    assert _put(conn, world["user"], username="ma-none", can_edit=True, can_remove=True) == 403
    assert _put(conn, world["user"], username="ma-user", can_edit=True, can_remove=True) == 403  # not to self
    assert _put(conn, world["user"], username="ma-none", can_edit=True, can_remove=False) == 204
    assert _put(conn, world["admin"], role="user", can_edit=False, can_remove=True) == 204
    assert _put(conn, world["user"], role="user", can_edit=True, can_remove=True) == 204  # remove kept: no change
    assert _put(conn, world["user"], role="user", can_edit=True, can_remove=False) == 204  # holds remove now


@pytest.mark.parametrize(("body", "status"), [
    ({"username": "ma-none", "role": "user", "can_edit": True, "can_remove": False}, 400),
    ({"can_edit": True, "can_remove": False}, 400),
    ({"username": "ma-none", "can_edit": "yes", "can_remove": False}, 400),
    ({"username": "nobody", "can_edit": True, "can_remove": False}, 404),
    ({"role": "nobody", "can_edit": True, "can_remove": False}, 404),
])
def test_put_member_input_errors(conn, world, store, body, status):
    assert _put(conn, world["admin"], **body) == status


def test_a_member_delete_ends_the_live_launches(conn, world, store):
    _approve(conn, world)
    launch_id = _launch(conn, world).body["launch_id"]
    member = _rows(conn, "SELECT id FROM miniapp_member WHERE artifact_code = %s", (CODE,))[0]["id"]
    req = _req(conn, world["admin"], params={"code": CODE, "id": member})
    assert routes.delete_member(req).status == 204
    assert routes.delete_member(req).status == 404
    assert _rows(conn, "SELECT revoked_at FROM launch WHERE id = %s", (launch_id,))[0]["revoked_at"]
    assert _launch(conn, world).status == 404


def test_a_member_removed_during_a_launch_gets_no_launch(conn, world, store, monkeypatch):
    # The route's own check passes; the removal commits before the launch's transaction
    # (it waits on the same retention lock in production). The re-check inside refuses.
    _approve(conn, world)
    resolve = activation._resolve

    def removed_meanwhile(*args, **kwargs):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM miniapp_member WHERE artifact_code = %s", (CODE,))
        conn.commit()
        return resolve(*args, **kwargs)

    monkeypatch.setattr(activation, "_resolve", removed_meanwhile)
    assert _launch(conn, world).status == 404
    assert not _rows(conn, "SELECT 1 FROM launch WHERE artifact_code = %s AND revoked_at IS NULL", (CODE,))


def _role_changed_meanwhile(conn, monkeypatch, user, role):
    """The route's check passes on the session's role; the stored role changes before the launch."""
    resolve = activation._resolve

    def changed(*args, **kwargs):
        with conn.cursor() as cur:
            cur.execute("UPDATE `user` SET role = %s WHERE id = %s", (role, user.id))
        return resolve(*args, **kwargs)

    monkeypatch.setattr(activation, "_resolve", changed)


def test_a_demoted_admin_with_the_launch_right_but_no_app_right_gets_no_launch(conn, world, store, monkeypatch):
    _approve(conn, world)  # role "user" holds artifact.launch and the tools; the admin is no member
    _role_changed_meanwhile(conn, monkeypatch, world["admin"], "user")
    assert _launch(conn, world, world["admin"]).status == 404
    assert not _rows(conn, "SELECT 1 FROM launch WHERE user_id = %s", (world["admin"].id,))


def test_a_role_change_that_ends_role_membership_gets_no_launch(conn, world, store, monkeypatch):
    _approve(conn, world)
    _member(conn, role="user")  # ma-none views through its role only
    with conn.cursor() as cur:  # the new role has every grant, but no membership
        cur.execute("INSERT IGNORE INTO role (code, label) VALUES ('maops', 'ma ops role')")
    try:
        accounts.add_grant(conn, "maops", "operation", "artifact.launch", agent_view_id=world["view"])
        for tool in ("jira_search", "jira_add_comment"):
            accounts.add_grant(conn, "maops", "tool", tool, agent_view_id=world["view"])
        _role_changed_meanwhile(conn, monkeypatch, world["none"], "maops")
        assert _launch(conn, world, world["none"]).status == 404
        assert not _rows(conn, "SELECT 1 FROM launch WHERE user_id = %s", (world["none"].id,))
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM role_grant WHERE role = 'maops'")
            cur.execute("DELETE FROM `user` WHERE role = 'maops'")
            cur.execute("DELETE FROM role WHERE code = 'maops'")


def test_a_failed_revoke_keeps_the_member(conn, world, store, monkeypatch):
    _approve(conn, world)
    launch_id = _launch(conn, world).body["launch_id"]
    member = _rows(conn, "SELECT id FROM miniapp_member WHERE artifact_code = %s", (CODE,))[0]["id"]

    def fail(cur, code):
        raise RuntimeError("revoke failed")

    monkeypatch.setattr(launches, "revoke_artifact_launches", fail)
    with pytest.raises(RuntimeError):
        routes.delete_member(_req(conn, world["admin"], params={"code": CODE, "id": member}))
    # One transaction: the member and the launch both stay, so a retry does the whole job.
    assert _rows(conn, "SELECT 1 FROM miniapp_member WHERE id = %s", (member,))
    assert not _rows(conn, "SELECT revoked_at FROM launch WHERE id = %s", (launch_id,))[0]["revoked_at"]
    monkeypatch.undo()
    assert routes.delete_member(_req(conn, world["admin"], params={"code": CODE, "id": member})).status == 204
    assert _rows(conn, "SELECT revoked_at FROM launch WHERE id = %s", (launch_id,))[0]["revoked_at"]


def test_the_catalogue_queries_do_not_grow_with_the_views(conn, world, store, monkeypatch):
    accounts.add_grant(conn, "user", "operation", "artifact.launch", agent_view_id=world["view2"])
    for tool in ("jira_search",):
        accounts.add_grant(conn, "user", "tool", tool, agent_view_id=world["view2"])
    _approve(conn, world)
    real = type(conn).cursor

    def count() -> tuple[int, list]:
        queries = []

        def counting(self, *a, **k):
            cur = real(self, *a, **k)
            execute = cur.execute
            cur.execute = lambda sql, *args: (queries.append(sql), execute(sql, *args))[1]
            return cur
        monkeypatch.setattr(type(conn), "cursor", counting)
        rows = routes.catalogue(_req(conn, world["user"])).body
        monkeypatch.setattr(type(conn), "cursor", real)
        return len(queries), [(v["agent_view_id"], v["execute"]) for v in rows[0]["views"]]

    one, views = count()
    assert views == [(world["view"], True)]
    _approve(conn, world, view=world["view2"])
    two, views = count()
    assert sorted(views) == sorted([(world["view"], True), (world["view2"], False)])  # not every action granted
    assert two == one


def test_settings_input_and_rights(conn, world, store):
    patch = lambda user, body: routes.update_app(_req(conn, user, params={"code": CODE}, body=body)).status  # noqa: E731
    assert patch(world["user"], {"title": "x"}) == 403
    for bad in ("not-an-icon", [], {}, 1, True):
        assert patch(world["admin"], {"icon": bad}) == 400
    assert patch(world["admin"], {"title": "t" * 256}) == 400
    assert patch(world["admin"], {"title": "  ", "icon": "mail"}) == 204
    got = routes.app(_req(conn, world["admin"], params={"code": CODE})).body
    assert (got["title"], got["icon"], got["default_title"]) == (None, "mail", "Notes app")
    assert patch(world["admin"], {"icon": ""}) == 204  # "" clears, as null does
    assert routes.app(_req(conn, world["admin"], params={"code": CODE})).body["icon"] is None


def test_a_non_admin_remove_holder_deletes_and_a_retry_finishes_it(conn, world, store):
    _member(conn, user=world["user"], edit=True)
    delete = lambda: routes.delete_app(_req(conn, world["user"], params={"code": CODE})).status  # noqa: E731
    assert delete() == 403
    _member(conn, user=world["user"], remove=True)
    store.crash_after_delete = True
    with pytest.raises(RuntimeError):
        delete()  # the store is gone, the row and its rights rows stay
    store.crash_after_delete = False
    assert delete() == 204  # the remove holder still holds remove: the retry finishes
    assert not _rows(conn, "SELECT 1 FROM versioned_artifact WHERE artifact_code = %s", (CODE,))
    assert delete() == 404  # the rights went with the row


# --- one execute rule: catalogue, POST /launches and the toolbox checker agree --------------

def _mint(conn, user, launch_id):
    with respx.mock:
        route = respx.post("http://toolbox:3001/internal/tools/jira_search:invoke").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {}}))
        session = sessions.create_session(conn, user)[0]
        assert invoke_launch_action(conn, session, launch_id, "jira_search", {}).status == 200
        return route.calls.last.request.headers["authorization"].removeprefix("Bearer ")


def _toolbox_accepts(conn, world, user) -> bool:
    """A launch made past the web check: does the real toolbox checker serve it?"""
    try:
        launch, code = launches.create_launch(conn, user, workspace_id=world["ws"], agent_view_id=world["view"],
                                              artifact_code=CODE, version_id=V1, allowed_actions=["jira_search"])
    except accounts.AccessError:
        return False
    assert launches.redeem(conn, launch.id, code)
    env = {**os.environ, "CORE_MODULES_DIR": str(_ROOT / "src/agento/modules")}
    out = subprocess.run(["node", str(Path(__file__).parent / "node" / "verify_capability.mjs")],
                         input=json.dumps({"token": _mint(conn, user, launch.id), "endpoint": "invoke"}),
                         capture_output=True, text=True, env=env, timeout=30, check=True)
    return json.loads(out.stdout) is not None


@pytest.mark.skipif(shutil.which("node") is None or not (_ROOT / "src/agento/toolbox/node_modules/mysql2").is_dir(),
                    reason="needs node and the toolbox's npm install")
@pytest.mark.parametrize(("case", "expected"), [
    ("member", True), ("role_member", True), ("admin", True), ("no_tool_grant", False),
    ("no_launch_grant", False), ("no_manifest", False), ("not_member", None),
])
def test_execute_rule_agrees(conn, world, store, case, expected):
    _approve(conn, world, actions=["jira_search"])
    user = world["admin"] if case == "admin" else world["user"]
    with conn.cursor() as cur:
        if case == "role_member":
            _member(conn, role="user")
        elif case == "no_tool_grant":  # a manifest action the approval left out
            cur.execute("DELETE FROM role_grant WHERE name = 'jira_add_comment'")
        elif case == "no_launch_grant":
            cur.execute("DELETE FROM role_grant WHERE role = 'user' AND name = 'artifact.launch'")
        elif case == "no_manifest":
            cur.execute("UPDATE miniapp_activation SET manifest_actions = NULL WHERE artifact_code = %s", (CODE,))
        elif case == "not_member":
            cur.execute("DELETE FROM miniapp_member WHERE artifact_code = %s", (CODE,))
    rows = routes.catalogue(_req(conn, user)).body
    shown = rows[0]["views"][0]["execute"] if rows else None
    status = _launch(conn, world, user=user).status
    assert (shown, status) == (expected, {True: 201, False: 403, None: 404}[expected])
    assert _toolbox_accepts(conn, world, user) is bool(expected)
