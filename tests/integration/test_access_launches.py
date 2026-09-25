"""Launch create / redeem / file authorization against a real MySQL (framework/access/launches.py)."""
# ruff: noqa: F811
from __future__ import annotations

import pytest

from agento.framework import config_resolver
from agento.framework.access import accounts, launches
from agento.framework.toolbox_capability import token_hash

from .access_fixtures import conn, scopes  # noqa: F401 (fixtures; tests take them as arguments, F811)

PASSWORD = "correct horse battery"
V1, V2 = "v-20260101-000000-aaaa", "v-20260102-000000-bbbb"


@pytest.fixture(autouse=True)
def _cap(monkeypatch):
    monkeypatch.setenv("CONFIG__WEB__LAUNCH__MAX_CONCURRENT", "5")


@pytest.fixture
def alice(conn, scopes):
    user = accounts.create_user(conn, "alice", "user", PASSWORD)
    accounts.add_grant(conn, "user", "operation", "artifact.launch", workspace_id=scopes["ws1"])
    return user


def _launch(conn, user, scopes, version=V1, code="app"):
    return launches.create_launch(conn, user, workspace_id=scopes["ws1"], agent_view_id=scopes["av1"],
                                  artifact_code=code, version_id=version)


def _redeemed(conn, user, scopes, version=V1):
    launch, code = _launch(conn, user, scopes, version)
    return launch, launches.redeem(conn, launch.id, code)[1]


def test_create_stores_hashes_and_the_no_manifest_constants(conn, scopes, alice):
    launch, code = _launch(conn, alice, scopes)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM launch WHERE id = %s", (launch.id,))
        row = cur.fetchone()
    assert code not in {str(v) for v in row.values()}
    assert row["exchange_code_hash"] == token_hash(code)
    assert row["manifest_fingerprint"] == launches.NO_MANIFEST_FINGERPRINT
    assert row["allowed_actions"] == "[]"
    assert (launch.artifact_code, launch.version_id, launch.agent_view_id) == ("app", V1, scopes["av1"])


def test_create_without_the_operation_grant_is_refused(conn, scopes):
    bob = accounts.create_user(conn, "bob", "user", PASSWORD)
    with pytest.raises(accounts.AccessError):
        _launch(conn, bob, scopes)
    accounts.add_grant(conn, "user", "operation", "artifact.launch", agent_view_id=scopes["av2"])
    with pytest.raises(accounts.AccessError):
        _launch(conn, bob, scopes)


def test_the_launch_over_the_cap_evicts_the_oldest(conn, scopes, alice):
    made = []
    for i in range(6):
        made.append(_launch(conn, alice, scopes)[0].id)
        with conn.cursor() as cur:  # created_at has 1 s precision
            cur.execute("UPDATE launch SET created_at = NOW() - INTERVAL %s MINUTE WHERE id = %s", (10 - i, made[-1]))
    live = {x.id for x in launches.list_launches(conn, alice)}
    assert live == set(made[1:])


def test_max_concurrent_resolution(conn, scopes, monkeypatch):
    monkeypatch.setenv("CONFIG__WEB__LAUNCH__MAX_CONCURRENT", "50")
    assert launches.max_concurrent(conn, scopes["ws1"]) == launches.MAX_CONCURRENT_CEILING
    for bad in ("0", "x", "-1", ""):
        monkeypatch.setenv("CONFIG__WEB__LAUNCH__MAX_CONCURRENT", bad)
        with pytest.raises(launches.AccessConfigError):
            launches.max_concurrent(conn, scopes["ws1"])
    monkeypatch.delenv("CONFIG__WEB__LAUNCH__MAX_CONCURRENT")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM core_config_data WHERE path = 'web/launch/max_concurrent'")
    # No ENV, no DB row: the web module's config.json, read without bootstrap().
    monkeypatch.setattr(config_resolver, "_config_defaults_cache", {})
    assert launches.max_concurrent(conn, scopes["ws1"]) == 5
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO core_config_data (scope, scope_id, path, value) VALUES"
                        " ('default', 0, 'web/launch/max_concurrent', '7'),"
                        " ('workspace', %s, 'web/launch/max_concurrent', '3')", (scopes["ws1"],))
        assert launches.max_concurrent(conn, scopes["ws1"]) == 3
        assert launches.max_concurrent(conn, scopes["ws2"]) == 7
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM core_config_data WHERE path = 'web/launch/max_concurrent'")


def test_redeem_is_single_use(conn, scopes, alice):
    launch, code = _launch(conn, alice, scopes)
    assert launches.redeem(conn, launch.id, "wrong") is None
    won = launches.redeem(conn, launch.id, code)
    assert won is not None and won[0].id == launch.id
    assert launches.redeem(conn, launch.id, code) is None
    with conn.cursor() as cur:
        cur.execute("SELECT token_hash FROM launch WHERE id = %s", (launch.id,))
        assert cur.fetchone()["token_hash"] == token_hash(won[1])


def test_redeem_loses_after_expiry_and_on_a_revoked_launch(conn, scopes, alice):
    late, late_code = _launch(conn, alice, scopes)
    with conn.cursor() as cur:
        cur.execute("UPDATE launch SET exchange_expires_at = NOW() - INTERVAL 1 SECOND WHERE id = %s", (late.id,))
    assert launches.redeem(conn, late.id, late_code) is None
    ended, ended_code = _launch(conn, alice, scopes)
    assert launches.revoke_launch(conn, alice, ended.id)
    assert launches.redeem(conn, ended.id, ended_code) is None


def test_authorize_files_pins_code_and_version(conn, scopes, alice):
    _launch_obj, token = _redeemed(conn, alice, scopes)
    assert launches.authorize_files(conn, [token], "app", V1)
    assert not launches.authorize_files(conn, [token], "app", V2)
    assert not launches.authorize_files(conn, [token], "other", V1)
    assert not launches.authorize_files(conn, ["not-a-token"], "app", V1)
    assert not launches.authorize_files(conn, [], "app", V1)


def test_two_launches_each_authorize_their_own_version(conn, scopes, alice):
    _a, token_a = _redeemed(conn, alice, scopes, V1)
    _b, token_b = _redeemed(conn, alice, scopes, V2)
    assert launches.authorize_files(conn, [token_a, token_b], "app", V1)
    assert launches.authorize_files(conn, [token_a, token_b], "app", V2)
    assert not launches.authorize_files(conn, [token_a], "app", V2)


def test_an_unredeemed_launch_authorizes_nothing(conn, scopes, alice):
    launch, _code = _launch(conn, alice, scopes)
    with conn.cursor() as cur:
        cur.execute("SELECT token_hash FROM launch WHERE id = %s", (launch.id,))
        assert cur.fetchone()["token_hash"] != token_hash(_code)
    assert not launches.authorize_files(conn, [_code], "app", V1)


@pytest.mark.parametrize("change", ["revoke", "deactivate", "set_role", "remove_grant"])
def test_access_changes_end_file_authorization(conn, scopes, alice, change):
    launch, token = _redeemed(conn, alice, scopes)
    if change == "revoke":
        assert launches.revoke_launch(conn, alice, launch.id)
    elif change == "deactivate":
        accounts.set_active(conn, alice.id, False)
    elif change == "set_role":
        accounts.set_role(conn, alice.id, "admin")
    else:
        grant = next(g for g in accounts.list_grants(conn, "user") if g["name"] == "artifact.launch")
        accounts.remove_grant(conn, grant["id"])
    assert not launches.authorize_files(conn, [token], "app", V1)
    assert launches.live_launch_ids(conn, [launch.id]) == set()


def test_revoke_launch_is_own_only_unless_admin(conn, scopes, alice):
    launch, _ = _launch(conn, alice, scopes)
    carol = accounts.create_user(conn, "carol", "user", PASSWORD)
    root = accounts.create_user(conn, "root", "admin", PASSWORD)
    assert not launches.revoke_launch(conn, carol, launch.id)
    assert launches.revoke_launch(conn, root, launch.id)
    assert not launches.revoke_launch(conn, root, launch.id)
