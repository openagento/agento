"""The account language against a real MySQL: create, backfill, profile write, and the config field."""
# ruff: noqa: F811
from __future__ import annotations

import io
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agento.framework import config_write, i18n, module_status
from agento.framework.access import accounts, sessions
from agento.framework.bootstrap import CORE_MODULES_DIR
from agento.framework.cli.access import UserCreateCommand
from agento.framework.harness import options_source
from agento.framework.module_loader import parse_module_dir
from agento.framework.setup import backfill_ui_locale
from agento.web import api

from .access_fixtures import conn  # noqa: F401 (fixture; tests take it as an argument, F811)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "i18n"
PASSWORD = "correct horse battery"
ENV = "CONFIG__ADMIN__LOCALE__LANGUAGE"
LANGUAGE_OWNER = parse_module_dir(Path(CORE_MODULES_DIR) / "admin")


def install(*fixture_modules: str) -> None:
    manifests = [LANGUAGE_OWNER, *(parse_module_dir(FIXTURES / name) for name in fixture_modules)]
    i18n.install(i18n.load(i18n.ordered_sources(manifests)))


@pytest.fixture(autouse=True)
def catalogs(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(module_status, "read_module_status", lambda *a, **kw: {})
    install()
    yield
    i18n.install(None)


def set_instance_language(conn, tag: str) -> None:
    with conn.cursor() as cur:
        cur.execute("REPLACE INTO core_config_data (scope, scope_id, path, value, encrypted)"
                    " VALUES ('default', 0, %s, %s, 0)", (i18n.LANGUAGE_PATH, tag))


def insert_user(conn, username: str, ui_locale: str | None) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO `user` (username, password_hash, role, ui_locale) VALUES (%s, NULL, 'user', %s)",
                    (username, ui_locale))
        return cur.lastrowid


def stored(conn) -> dict[str, str | None]:
    with conn.cursor() as cur:
        cur.execute("SELECT username, ui_locale FROM `user` ORDER BY username")
        return {row["username"]: row["ui_locale"] for row in cur.fetchall()}


def request(conn, user: accounts.User, body=None) -> api.Request:
    session = sessions.Session(id="sid", user=user,
                               expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))
    return api.Request(method="POST", path="/", headers={}, body=b"", cookies={}, origins=None, conn=conn,
                       session=session, session_token="token", json=body)


def test_the_instance_language_follows_a_real_config_write_and_new_accounts_get_it(conn, monkeypatch):
    before = accounts.create_user(conn, "before", "user", PASSWORD)
    assert api.panel_bootstrap(request(conn, before)).body["locale"] == "en"
    assert api.get_session(request(conn, before)).body["locale"]["effective"] == "en"

    config_write.save_config(conn, i18n.LANGUAGE_PATH, "pl", scope="default", scope_id=0, allow_secret=False)

    bootstrap = api.panel_bootstrap(request(conn, before)).body
    assert bootstrap["locale"] == "pl"
    assert bootstrap["messages"]["panel"] == i18n.current().login["pl"].messages
    admin = accounts.create_user(conn, "root", "admin", PASSWORD)
    created = api.admin_create_user(request(conn, admin, {"username": "by-api", "role": "user",
                                                           "password": PASSWORD}))
    assert (created.status, created.body["ui_locale"]) == (201, "pl")
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    UserCreateCommand().run(conn, Namespace(username="by-cli", role="user"))

    assert stored(conn) == {"before": "en", "by-api": "pl", "by-cli": "pl", "root": "pl"}
    kept = api.get_session(request(conn, accounts.get_user(conn, before.id))).body
    assert (kept["user"]["ui_locale"], kept["locale"]["effective"], kept["locale"]["instance"]) == ("en", "en", "pl")


def test_the_config_field_takes_only_a_registered_language(conn):
    with pytest.raises(config_write.ConfigWriteError) as refused:
        config_write.save_config(conn, i18n.LANGUAGE_PATH, "de", scope="default", scope_id=0, allow_secret=False)
    assert refused.value.code == "CONFIG_VALUE_NOT_ALLOWED"

    install("pack_de")
    config_write.save_config(conn, i18n.LANGUAGE_PATH, "de", scope="default", scope_id=0, allow_secret=False)

    assert i18n.instance_language(conn) == "de"
    assert {"value": "de", "label": "Deutsch"} in options_source.resolve_options("panel_languages")


def test_the_field_cannot_be_set_below_the_default_scope(conn):
    with pytest.raises(config_write.ConfigWriteError) as refused:
        config_write.save_config(conn, i18n.LANGUAGE_PATH, "pl", scope="workspace", scope_id=1, allow_secret=False)

    assert refused.value.code == "CONFIG_SCOPE_NOT_ALLOWED"


def test_the_backfill_fills_only_empty_rows_and_a_second_run_changes_nothing(conn):
    set_instance_language(conn, "pl")
    insert_user(conn, "empty-a", None)
    insert_user(conn, "empty-b", None)
    insert_user(conn, "chose-en", "en")
    insert_user(conn, "chose-de", "de")
    expected = {"empty-a": "pl", "empty-b": "pl", "chose-en": "en", "chose-de": "de"}

    backfill_ui_locale(conn)
    assert stored(conn) == expected

    set_instance_language(conn, "en")
    backfill_ui_locale(conn)
    assert stored(conn) == expected


def test_a_failed_config_read_stops_the_backfill_and_writes_nothing(conn, monkeypatch):
    insert_user(conn, "empty", None)

    def broken(_conn, *, strict):
        assert strict
        raise RuntimeError("db down")

    monkeypatch.setattr(i18n, "_stored_instance_language", broken)

    with pytest.raises(RuntimeError):
        backfill_ui_locale(conn)
    with pytest.raises(RuntimeError):
        accounts.create_user(conn, "new", "user", PASSWORD)
    assert stored(conn) == {"empty": None}


def test_a_configured_language_with_no_pack_gives_english_to_the_backfill_and_to_a_new_account(conn):
    set_instance_language(conn, "de")
    insert_user(conn, "empty", None)

    backfill_ui_locale(conn)
    accounts.create_user(conn, "new", "user", PASSWORD)

    assert stored(conn) == {"empty": "en", "new": "en"}


def test_a_stored_language_with_no_pack_stays_stored_and_the_effective_language_is_the_instance(conn):
    set_instance_language(conn, "pl")
    user_id = insert_user(conn, "hans", "de")

    without_pack = api.get_session(request(conn, accounts.get_user(conn, user_id))).body
    install("pack_de")
    with_pack = api.get_session(request(conn, accounts.get_user(conn, user_id))).body

    assert (without_pack["user"]["ui_locale"], without_pack["locale"]["effective"]) == ("de", "pl")
    assert (with_pack["user"]["ui_locale"], with_pack["locale"]["effective"]) == ("de", "de")
    assert {"tag": "de", "name": "Deutsch"} in with_pack["locale"]["available"]
    assert stored(conn) == {"hans": "de"}


def test_the_profile_write_stores_the_tag_and_revokes_no_session(conn):
    user = accounts.create_user(conn, "alice", "user", PASSWORD)
    session, token = sessions.sign_in(conn, "alice", PASSWORD)
    assert session.user.ui_locale == "en"

    answer = api.set_profile_locale(request(conn, session.user, {"ui_locale": "pl"}))

    assert (answer.status, answer.body["ui_locale"], answer.body["locale"]["effective"]) == (200, "pl", "pl")
    restored = sessions.lookup_session(conn, token)
    assert restored is not None and restored.user.ui_locale == "pl"
    again, _token = sessions.sign_in(conn, "alice", PASSWORD)
    assert again.user.ui_locale == "pl"
    assert [u.ui_locale for u in accounts.list_users(conn)] == ["pl"]
    assert accounts.get_user_by_username(conn, "alice").ui_locale == accounts.get_user(conn, user.id).ui_locale == "pl"
