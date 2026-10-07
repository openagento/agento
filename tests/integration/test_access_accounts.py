"""Users, roles and role grants against a real MySQL (framework/access/accounts.py)."""
# ruff: noqa: F811
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agento.framework.access import accounts
from agento.framework.access.accounts import AccessError

from .access_fixtures import conn, scopes  # noqa: F401 (fixtures; tests take them as arguments, F811)
from .conftest import _test_connection

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "role_grant_v1.json").read_text())
PASSWORD = "correct horse battery"


def _insert_raw_grant(conn, grant, ids):
    # Raw insert: the fixture holds rows add_grant refuses (both/neither scope, fake tools).
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_grant (role, grant_kind, name, workspace_id, agent_view_id) VALUES (%s,%s,%s,%s,%s)",
            (grant["role"], grant["grant_kind"], grant["name"],
             ids.get(grant["workspace"]), ids.get(grant["agent_view"])),
        )


def _session_and_launch(conn, user_id, workspace_id, agent_view_id=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO session (id, token_hash, user_id, expires_at) VALUES (%s, %s, %s, NOW() + INTERVAL 1 HOUR)",
            (f"s{user_id}", f"{user_id:064d}", user_id),
        )
        cur.execute(
            "INSERT INTO launch (id, token_hash, user_id, artifact_code, version_id, manifest_fingerprint,"
            " allowed_actions, workspace_id, agent_view_id, expires_at)"
            " VALUES (%s, %s, %s, 'a', 'v', %s, '[]', %s, %s, NOW() + INTERVAL 1 HOUR)",
            (f"l{user_id}", f"{user_id:064d}", user_id, "0" * 64, workspace_id, agent_view_id),
        )


def _revoked(conn, user_id):
    with conn.cursor() as cur:
        cur.execute("SELECT revoked_at FROM session WHERE user_id = %s", (user_id,))
        s = cur.fetchone()["revoked_at"] is not None
        cur.execute("SELECT revoked_at FROM launch WHERE user_id = %s", (user_id,))
        return s, cur.fetchone()["revoked_at"] is not None


@pytest.mark.parametrize("name", ["Admin", "a b", "x" * 65, "-lead", "", "zażółć"])
def test_create_user_rejects_bad_username(conn, name):
    with pytest.raises(AccessError):
        accounts.create_user(conn, name, "user", PASSWORD)


def test_create_user_rejects_unknown_role_and_duplicate(conn):
    with pytest.raises(AccessError):
        accounts.create_user(conn, "alice", "owner", PASSWORD)
    accounts.create_user(conn, "alice", "user", PASSWORD)
    with pytest.raises(AccessError, match="exists"):
        accounts.create_user(conn, "alice", "user", PASSWORD)


def test_authenticate(conn):
    alice = accounts.create_user(conn, "alice", "user", PASSWORD)
    assert accounts.authenticate(conn, "alice", PASSWORD) == alice
    assert accounts.authenticate(conn, "alice", "wrong password!!") is None
    assert accounts.authenticate(conn, "ALICE", PASSWORD) is None
    assert accounts.authenticate(conn, "nobody", PASSWORD) is None
    accounts.create_user(conn, "nopass", "user", None)
    assert accounts.authenticate(conn, "nopass", PASSWORD) is None
    accounts.set_active(conn, alice.id, False)
    assert accounts.authenticate(conn, "alice", PASSWORD) is None


def test_get_user_by_username_skips_query_for_bad_grammar(conn):
    accounts.create_user(conn, "alice", "user", PASSWORD)
    assert accounts.get_user_by_username(conn, "alice").username == "alice"
    assert accounts.get_user_by_username(conn, "Alice") is None


def test_set_role_revokes_only_that_users_sessions_and_launches(conn, scopes):
    alice = accounts.create_user(conn, "alice", "user", PASSWORD)
    bob = accounts.create_user(conn, "bob", "user", PASSWORD)
    _session_and_launch(conn, alice.id, scopes["ws1"])
    _session_and_launch(conn, bob.id, scopes["ws1"])
    accounts.set_role(conn, alice.id, "admin")
    assert accounts.get_user(conn, alice.id).role == "admin"
    assert _revoked(conn, alice.id) == (True, True)
    assert _revoked(conn, bob.id) == (False, False)


def test_set_password_revokes_sessions(conn, scopes):
    alice = accounts.create_user(conn, "alice", "user", PASSWORD)
    _session_and_launch(conn, alice.id, scopes["ws1"])
    accounts.set_password(conn, alice.id, "another long password")
    assert _revoked(conn, alice.id) == (True, False)
    assert accounts.authenticate(conn, "alice", "another long password") == alice


def test_actor_must_be_active_admin(conn):
    admin = accounts.create_user(conn, "root", "admin", PASSWORD)
    plain = accounts.create_user(conn, "alice", "user", PASSWORD)
    with pytest.raises(AccessError):
        accounts.set_role(conn, plain.id, "admin", actor_id=plain.id)
    with pytest.raises(AccessError):
        accounts.create_user(conn, "bob", "user", PASSWORD, actor_id=plain.id)
    accounts.set_active(conn, admin.id, False)
    with pytest.raises(AccessError):
        accounts.set_role(conn, plain.id, "admin", actor_id=admin.id)
    assert accounts.get_user(conn, plain.id).role == "user"


def test_unknown_target_is_refused(conn):
    with pytest.raises(AccessError, match="not found"):
        accounts.set_role(conn, 999999, "user")


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: f"{c['role']}-{c['grant_kind']}-{c['workspace']}-{c['agent_view']}")
def test_grant_evaluation_matches_shared_fixture(conn, scopes, case):
    for grant in FIXTURE["grants"]:
        _insert_raw_grant(conn, grant, scopes)
    got = accounts._granted(conn, case["role"], case["grant_kind"], scopes.get(case["workspace"]),
                            scopes.get(case["agent_view"]))
    assert got == case["expected"]
    if case["grant_kind"] == "tool":
        assert accounts.permitted_tools(conn, case["role"], scopes.get(case["workspace"]),
                                        scopes.get(case["agent_view"])) == case["expected"]
    else:
        assert accounts.has_operation(conn, case["role"], "artifact.launch", scopes.get(case["workspace"]),
                                      scopes.get(case["agent_view"])) == ("artifact.launch" in case["expected"])


def test_visible_agent_views_and_can_reach(conn, scopes):
    _insert_raw_grant(conn, {"role": "user", "grant_kind": "tool", "name": "t", "workspace": None,
                             "agent_view": "av1"}, scopes)
    admin = accounts.create_user(conn, "root", "admin", PASSWORD)
    alice = accounts.create_user(conn, "alice", "user", PASSWORD)
    seen = {v["id"] for v in accounts.visible_agent_views(conn, alice)}
    assert scopes["av1"] in seen and scopes["av2"] not in seen
    assert {scopes["av1"], scopes["av2"]} <= {v["id"] for v in accounts.visible_agent_views(conn, admin)}
    assert accounts.can_reach(conn, alice, workspace_id=scopes["ws1"], agent_view_id=scopes["av1"])
    assert not accounts.can_reach(conn, alice, workspace_id=scopes["ws2"], agent_view_id=scopes["av2"])
    assert accounts.can_reach(conn, admin, workspace_id=scopes["ws2"], agent_view_id=scopes["av2"])


def test_add_grant_validation(conn, scopes):
    with pytest.raises(AccessError, match="declares"):
        accounts.add_grant(conn, "user", "tool", "no_such_tool", agent_view_id=scopes["av1"])
    with pytest.raises(AccessError, match="operation"):
        accounts.add_grant(conn, "user", "operation", "users.manage", agent_view_id=scopes["av1"])
    with pytest.raises(AccessError, match="exactly one"):
        accounts.add_grant(conn, "user", "operation", "artifact.launch")
    with pytest.raises(AccessError, match="exactly one"):
        accounts.add_grant(conn, "user", "operation", "artifact.launch",
                           workspace_id=scopes["ws1"], agent_view_id=scopes["av1"])
    with pytest.raises(AccessError):
        accounts.add_grant(conn, "owner", "operation", "artifact.launch", agent_view_id=scopes["av1"])
    gid = accounts.add_grant(conn, "user", "tool", "versioned_artifact_get_current", agent_view_id=scopes["av1"])
    assert accounts.add_grant(conn, "user", "tool", "versioned_artifact_get_current",
                              agent_view_id=scopes["av1"]) == gid


def test_concurrent_duplicate_grants_insert_one_row(conn, scopes):
    barrier, errors = threading.Barrier(6), []

    def run():
        c = _test_connection(autocommit=True)
        try:
            barrier.wait()
            accounts.add_grant(c, "user", "operation", "artifact.launch", workspace_id=scopes["ws2"])
        except Exception as exc:
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    rows = [g for g in accounts.list_grants(conn, "user") if g["workspace_id"] == scopes["ws2"]]
    assert len(rows) == 1


def test_remove_grant_revokes_launches_of_that_role_in_that_scope(conn, scopes):
    alice = accounts.create_user(conn, "alice", "user", PASSWORD)
    root = accounts.create_user(conn, "root", "admin", PASSWORD)
    other = accounts.create_user(conn, "carol", "user", PASSWORD)
    _session_and_launch(conn, alice.id, scopes["ws1"], scopes["av1"])
    _session_and_launch(conn, root.id, scopes["ws1"], scopes["av1"])
    _session_and_launch(conn, other.id, scopes["ws2"], scopes["av2"])
    gid = accounts.add_grant(conn, "user", "operation", "artifact.launch", workspace_id=scopes["ws1"])
    with pytest.raises(AccessError):
        accounts.remove_grant(conn, gid, actor_id=alice.id)
    accounts.remove_grant(conn, gid, actor_id=root.id)
    assert accounts.list_grants(conn) == []
    assert _revoked(conn, alice.id) == (False, True)
    assert _revoked(conn, root.id) == (False, False)
    assert _revoked(conn, other.id) == (False, False)
    with pytest.raises(AccessError, match="not found"):
        accounts.remove_grant(conn, gid)


def test_may():
    admin = accounts.User(1, "root", "admin", True)
    assert accounts.may(admin, "config.write")
    assert not accounts.may(admin, "artifact.launch")
    assert not accounts.may(accounts.User(2, "a", "user", True), "config.write")
    assert not accounts.may(accounts.User(3, "b", "admin", False), "config.write")


def test_role_crud(conn):
    root = accounts.create_user(conn, "root", "admin", PASSWORD)
    plain = accounts.create_user(conn, "alice", "user", PASSWORD)
    with pytest.raises(AccessError, match="not allowed"):
        accounts.create_role(conn, "support", "Support", actor_id=plain.id)
    for code in ("Support", "s", "9lives", "x" * 17, "a-b"):
        with pytest.raises(AccessError, match="code must match"):
            accounts.create_role(conn, code, "Support")
    with pytest.raises(AccessError, match="label"):
        accounts.create_role(conn, "support", "  ")
    assert accounts.create_role(conn, "support", " Support ", actor_id=root.id)["label"] == "Support"
    with pytest.raises(AccessError, match="code already exists"):
        accounts.create_role(conn, "support", "Other")
    with pytest.raises(AccessError, match="label already exists"):
        accounts.create_role(conn, "other", "Support")
    accounts.rename_role(conn, "support", "Support desk", actor_id=root.id)
    with pytest.raises(AccessError, match="role not found"):
        accounts.rename_role(conn, "nobody", "Nobody")
    with pytest.raises(AccessError, match="label already exists"):
        accounts.rename_role(conn, "support", "User")
    roles = {r["code"]: r for r in accounts.list_roles(conn)}
    assert roles["support"] == {"code": "support", "label": "Support desk", "builtin": False, "users": 0, "scopes": 0}
    assert roles["admin"]["builtin"] and roles["admin"]["users"] == 1 and roles["user"]["users"] == 1


def test_delete_role(conn, scopes):
    accounts.create_role(conn, "support", "Support")
    with pytest.raises(AccessError, match="built-in"):
        accounts.delete_role(conn, "user")
    with pytest.raises(AccessError, match="role not found"):
        accounts.delete_role(conn, "nobody")
    with pytest.raises(AccessError, match="role not found"):
        accounts.delete_role(conn, "User")  # the column collation is case-insensitive
    bob = accounts.create_user(conn, "bob", "support", PASSWORD)
    accounts.add_grant(conn, "support", "operation", "artifact.launch", agent_view_id=scopes["av1"])
    with pytest.raises(AccessError, match="1 user has this role"):
        accounts.delete_role(conn, "support")
    accounts.set_role(conn, bob.id, "user")
    accounts.delete_role(conn, "support")
    assert "support" not in {r["code"] for r in accounts.list_roles(conn)}
    assert accounts.list_grants(conn, "support") == []


def test_unknown_role_is_refused_by_every_writer(conn, scopes):
    with pytest.raises(AccessError, match="unknown role"):
        accounts.create_user(conn, "bob", "support", PASSWORD)
    alice = accounts.create_user(conn, "alice", "user", PASSWORD)
    with pytest.raises(AccessError, match="unknown role"):
        accounts.set_role(conn, alice.id, "support")
    with pytest.raises(AccessError, match="unknown role"):
        accounts.add_grant(conn, "support", "operation", "artifact.launch", agent_view_id=scopes["av1"])
    with pytest.raises(AccessError, match="unknown role"):
        accounts.set_role_grants(conn, "support", agent_view_id=scopes["av1"], tools=[], operations=[])
    accounts.create_role(conn, "support", "Support")
    accounts.set_role(conn, alice.id, "support")
    assert accounts.get_user(conn, alice.id).role == "support"


def test_role_scopes_and_list_counts(conn, scopes):
    accounts.create_role(conn, "support", "Support")
    accounts.add_grant(conn, "support", "operation", "artifact.launch", workspace_id=scopes["ws1"])
    accounts.add_grant(conn, "support", "tool", "versioned_artifact_get_current", workspace_id=scopes["ws1"])
    accounts.add_grant(conn, "support", "tool", "versioned_artifact_get_current", agent_view_id=scopes["av2"])
    _insert_raw_grant(conn, {"role": "support", "grant_kind": "tool", "name": "t", "workspace": None,
                             "agent_view": None}, scopes)  # no scope: matches nothing, counted nowhere
    assert accounts.role_scopes(conn, "support") == [
        {"workspace_id": None, "agent_view_id": scopes["av2"], "tools": 1, "operations": 0},
        {"workspace_id": scopes["ws1"], "agent_view_id": None, "tools": 1, "operations": 1},
    ]
    assert {r["code"]: r["scopes"] for r in accounts.list_roles(conn)}["support"] == 2


def _rows(conn, role, scopes, scope):
    key = "agent_view_id" if scope.startswith("av") else "workspace_id"
    return sorted((g["grant_kind"], g["name"]) for g in accounts.list_grants(conn, role) if g[key] == scopes[scope])


def test_set_role_grants_replaces_one_scope(conn, scopes):
    root = accounts.create_user(conn, "root", "admin", PASSWORD)
    alice = accounts.create_user(conn, "alice", "user", PASSWORD)
    other = accounts.create_user(conn, "carol", "user", PASSWORD)
    with pytest.raises(AccessError, match="not allowed"):
        accounts.set_role_grants(conn, "user", agent_view_id=scopes["av1"], tools=[], operations=[],
                                 actor_id=alice.id)
    got = accounts.set_role_grants(conn, "user", agent_view_id=scopes["av1"], actor_id=root.id,
                                   tools=["versioned_artifact_get_current", "miniapp_list"],
                                   operations=["artifact.launch"])
    assert got == {"added": 3, "removed": 0}
    accounts.add_grant(conn, "user", "tool", "miniapp_list", agent_view_id=scopes["av2"])
    _session_and_launch(conn, alice.id, scopes["ws1"], scopes["av1"])
    _session_and_launch(conn, other.id, scopes["ws2"], scopes["av2"])
    same = accounts.set_role_grants(conn, "user", agent_view_id=scopes["av1"], operations=["artifact.launch"],
                                    tools=["miniapp_list", "versioned_artifact_get_current"])
    assert same == {"added": 0, "removed": 0}
    assert _revoked(conn, alice.id) == (False, False)  # nothing removed: no launch ends
    got = accounts.set_role_grants(conn, "user", agent_view_id=scopes["av1"], tools=["miniapp_list"],
                                   operations=[])
    assert got == {"added": 0, "removed": 2}
    assert _rows(conn, "user", scopes, "av1") == [("tool", "miniapp_list")]
    assert _rows(conn, "user", scopes, "av2") == [("tool", "miniapp_list")]  # other scope untouched
    assert _revoked(conn, alice.id) == (False, True)
    assert _revoked(conn, other.id) == (False, False)


def test_set_role_grants_skips_names_the_workspace_already_grants(conn, scopes):
    accounts.add_grant(conn, "user", "tool", "miniapp_list", workspace_id=scopes["ws1"])
    accounts.add_grant(conn, "user", "operation", "artifact.launch", agent_view_id=scopes["av1"])
    accounts.add_grant(conn, "user", "operation", "artifact.launch", workspace_id=scopes["ws1"])
    got = accounts.set_role_grants(conn, "user", agent_view_id=scopes["av1"], tools=["miniapp_list"],
                                   operations=["artifact.launch"])
    # Not inserted at the view (redundant); the redundant view row already there stays.
    assert got == {"added": 0, "removed": 0}
    assert _rows(conn, "user", scopes, "av1") == [("operation", "artifact.launch")]
    alice = accounts.create_user(conn, "alice", "user", PASSWORD)
    _session_and_launch(conn, alice.id, scopes["ws1"], scopes["av1"])
    # The tree never sends a locked (inherited) leaf: the redundant row stays, no launch ends.
    assert accounts.set_role_grants(conn, "user", agent_view_id=scopes["av1"], tools=[], operations=[]) == {
        "added": 0, "removed": 0}
    assert _rows(conn, "user", scopes, "av1") == [("operation", "artifact.launch")]
    assert _revoked(conn, alice.id) == (False, False)


def test_set_role_grants_keeps_rows_of_a_disabled_module(conn, scopes, monkeypatch):
    accounts.add_grant(conn, "user", "tool", "miniapp_list", agent_view_id=scopes["av1"])
    enabled = accounts.declared_tools(enabled_only=True) - {"miniapp_list"}
    real = accounts.declared_tools
    monkeypatch.setattr(accounts, "declared_tools", lambda enabled_only=False: enabled if enabled_only else real())
    assert accounts.set_role_grants(conn, "user", agent_view_id=scopes["av1"], tools=[], operations=[]) == {
        "added": 0, "removed": 0}
    assert _rows(conn, "user", scopes, "av1") == [("tool", "miniapp_list")]


@pytest.mark.parametrize("kwargs,message", [
    ({"tools": [], "operations": []}, "exactly one"),
    ({"workspace_id": 1, "agent_view_id": 1, "tools": [], "operations": []}, "exactly one"),
    ({"agent_view_id": 1, "tools": ["no_such_tool"], "operations": []}, "declares"),
    ({"agent_view_id": 1, "tools": [], "operations": ["users.manage"]}, "operation must be one of"),
    ({"agent_view_id": 1, "tools": "miniapp_list", "operations": []}, "list of names"),
    ({"agent_view_id": 1, "tools": [f"t{i}" for i in range(1001)], "operations": []}, "at most 1000"),
])
def test_set_role_grants_validation(conn, kwargs, message):
    with pytest.raises(AccessError, match=message):
        accounts.set_role_grants(conn, "user", **kwargs)


def test_set_role_grants_unknown_view_is_not_found(conn):
    with pytest.raises(AccessError, match="agent_view not found"):
        accounts.set_role_grants(conn, "user", agent_view_id=999_999_999, tools=[], operations=[])
