"""Launch and access writes under concurrency: threads on separate connections, real MySQL."""
# ruff: noqa: F811
from __future__ import annotations

import threading
from functools import partial

import pytest

from agento.framework.access import accounts, launches

from .access_fixtures import conn, scopes  # noqa: F401 (fixtures; tests take them as arguments, F811)
from .conftest import _test_connection

PASSWORD = "correct horse battery"
V1 = "v-20260101-000000-aaaa"


def _race(*calls):
    """Run each call on its own connection at the same moment; return (results, errors)."""
    barrier, results, errors = threading.Barrier(len(calls)), [None] * len(calls), []

    def run(i, fn):
        c = _test_connection(autocommit=True)
        try:
            barrier.wait()
            results[i] = fn(c)
        except accounts.AccessError as exc:
            results[i] = exc
        except Exception as exc:
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def _launch_as(user, scopes):
    return lambda c: launches.create_launch(c, user, workspace_id=scopes["ws1"], agent_view_id=scopes["av1"],
                                            artifact_code="app", version_id=V1)


def _live_launches_without_access(conn):
    """Live launches whose user's current role lacks artifact.launch for the launch's scope."""
    with conn.cursor() as cur:
        cur.execute("SELECT l.id, l.workspace_id, l.agent_view_id, u.role FROM launch l"
                    " JOIN `user` u ON u.id = l.user_id WHERE l.revoked_at IS NULL AND l.expires_at > NOW()")
        rows = cur.fetchall()
    return [r["id"] for r in rows
            if not accounts.has_operation(conn, r["role"], "artifact.launch", r["workspace_id"], r["agent_view_id"])]


def _reset(conn):
    with conn.cursor() as cur:
        for table in ("launch", "session", "role_grant", "`user`"):
            cur.execute(f"DELETE FROM {table}")


def test_concurrent_creates_respect_the_cap(conn, scopes, monkeypatch):
    monkeypatch.setenv("CONFIG__WEB__LAUNCH__MAX_CONCURRENT", "3")
    alice = accounts.create_user(conn, "alice", "user", PASSWORD)
    accounts.add_grant(conn, "user", "operation", "artifact.launch", workspace_id=scopes["ws1"])
    results, errors = _race(*[_launch_as(alice, scopes)] * 10)
    assert errors == []
    assert all(isinstance(r, tuple) for r in results)
    assert len(launches.list_launches(conn, alice)) == 3


@pytest.mark.parametrize("change", ["set_role", "remove_grant"])
def test_create_racing_an_access_change_leaves_no_unauthorized_launch(conn, scopes, monkeypatch, change):
    monkeypatch.setenv("CONFIG__WEB__LAUNCH__MAX_CONCURRENT", "5")
    for _ in range(20):
        _reset(conn)
        alice = accounts.create_user(conn, "alice", "user", PASSWORD)
        gid = accounts.add_grant(conn, "user", "operation", "artifact.launch", workspace_id=scopes["ws1"])
        if change == "set_role":
            revoke = partial(accounts.set_role, user_id=alice.id, role="admin")
        else:
            revoke = partial(accounts.remove_grant, grant_id=gid)
        _results, errors = _race(_launch_as(alice, scopes), revoke)
        assert errors == []
        assert _live_launches_without_access(conn) == []


def test_two_admins_demoting_each_other_do_not_deadlock(conn):
    for _ in range(20):
        _reset(conn)
        a = accounts.create_user(conn, "root-a", "admin", PASSWORD)
        b = accounts.create_user(conn, "root-b", "admin", PASSWORD)
        results, errors = _race(partial(accounts.set_role, user_id=b.id, role="user", actor_id=a.id),
                                partial(accounts.set_role, user_id=a.id, role="user", actor_id=b.id))
        assert errors == []
        roles = {accounts.get_user(conn, a.id).role, accounts.get_user(conn, b.id).role}
        assert roles == {"admin", "user"}
        assert sum(isinstance(r, accounts.AccessError) for r in results) == 1


def test_two_grant_removals_on_one_role_do_not_deadlock(conn, scopes):
    for _ in range(20):
        _reset(conn)
        accounts.create_user(conn, "alice", "user", PASSWORD)
        g1 = accounts.add_grant(conn, "user", "operation", "artifact.launch", workspace_id=scopes["ws1"])
        g2 = accounts.add_grant(conn, "user", "operation", "artifact.launch", workspace_id=scopes["ws2"])
        _results, errors = _race(partial(accounts.remove_grant, grant_id=g1),
                                 partial(accounts.remove_grant, grant_id=g2))
        assert errors == []
        assert accounts.list_grants(conn) == []


def test_two_admins_adding_the_same_grant_get_one_row(conn, scopes):
    a = accounts.create_user(conn, "root-a", "admin", PASSWORD)
    b = accounts.create_user(conn, "root-b", "admin", PASSWORD)
    add = lambda actor: lambda c: accounts.add_grant(  # noqa: E731
        c, "user", "operation", "artifact.launch", agent_view_id=scopes["av1"], actor_id=actor)
    results, errors = _race(add(a.id), add(b.id))
    assert errors == []
    assert len(accounts.list_grants(conn)) == 1
    assert results[0] == results[1] == accounts.list_grants(conn)[0]["id"]


def test_concurrent_redeems_yield_exactly_one_token(conn, scopes, monkeypatch):
    monkeypatch.setenv("CONFIG__WEB__LAUNCH__MAX_CONCURRENT", "5")
    alice = accounts.create_user(conn, "alice", "user", PASSWORD)
    accounts.add_grant(conn, "user", "operation", "artifact.launch", workspace_id=scopes["ws1"])
    launch, code = _launch_as(alice, scopes)(conn)
    results, errors = _race(*[lambda c: launches.redeem(c, launch.id, code)] * 8)
    assert errors == []
    assert sum(r is not None for r in results) == 1


@pytest.mark.parametrize("change", ["set_role", "set_password", "deactivate"])
def test_sign_in_racing_a_revocation_leaves_no_live_session(conn, change):
    from agento.framework.access import sessions

    for _ in range(20):
        _reset(conn)
        alice = accounts.create_user(conn, "alice", "user", PASSWORD)
        revoke = {
            "set_role": partial(accounts.set_role, user_id=alice.id, role="admin"),
            "set_password": partial(accounts.set_password, user_id=alice.id, password="another long password"),
            "deactivate": partial(accounts.set_active, user_id=alice.id, active=False),
        }[change]
        results, errors = _race(lambda c: sessions.sign_in(c, "alice", PASSWORD), revoke)
        assert errors == []
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM session WHERE revoked_at IS NULL")
            live = cur.fetchone()["n"]
        # A sign-in after a role change is valid, but it must have read the new role under the lock.
        # After a password change or a deactivation no sign-in with the old password may survive.
        assert live == 0 or (change == "set_role" and results[0][0].user.role == "admin")


def test_update_user_applies_every_field_or_none(conn):
    admin = accounts.create_user(conn, "root", "admin", PASSWORD)
    alice = accounts.create_user(conn, "alice", "user", PASSWORD)
    with pytest.raises(accounts.AccessError):
        accounts.update_user(conn, alice.id, role="admin", password="short", actor_id=admin.id)
    assert accounts.get_user(conn, alice.id).role == "user"
    # Self-demotion with a password change: one transaction, so both land (no partial commit, no 403).
    accounts.update_user(conn, admin.id, role="user", password="another long password", actor_id=admin.id)
    assert accounts.get_user(conn, admin.id).role == "user"
    assert accounts.authenticate(conn, "root", "another long password") is not None


def test_update_user_by_an_actor_demoted_concurrently_changes_nothing(conn):
    for _ in range(20):
        _reset(conn)
        a = accounts.create_user(conn, "root-a", "admin", PASSWORD)
        b = accounts.create_user(conn, "root-b", "admin", PASSWORD)
        alice = accounts.create_user(conn, "alice", "user", PASSWORD)
        _results, errors = _race(
            partial(accounts.set_role, user_id=b.id, role="user", actor_id=a.id),
            partial(accounts.update_user, user_id=alice.id, role="admin", active=False, actor_id=b.id))
        assert errors == []
        after = accounts.get_user(conn, alice.id)
        assert (after.role, after.is_active) in {("user", True), ("admin", False)}
