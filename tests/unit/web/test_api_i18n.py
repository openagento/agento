"""The panel language routes: bootstrap, catalog, profile language, and the session's language block."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from agento.framework import i18n, module_status
from agento.framework.access import accounts, acl, sessions
from agento.framework.admin import data
from agento.framework.bootstrap import CORE_MODULES_DIR
from agento.framework.module_loader import parse_module_dir
from agento.web import api
from tests.module_writer import write_module

from .conftest import TOKEN, panel_headers, sign_in_as

FIXTURES = Path(__file__).parents[2] / "fixtures" / "i18n"
COOKIES = {"__Host-agento-session": TOKEN}
ENV = "CONFIG__ADMIN__LOCALE__LANGUAGE"
LANGUAGE_OWNER = parse_module_dir(Path(CORE_MODULES_DIR) / "admin")


def install_manifests(*manifests) -> i18n.Catalogs:
    catalogs = i18n.load(i18n.ordered_sources([LANGUAGE_OWNER, *manifests]))
    i18n.install(catalogs)
    return catalogs


def install(*fixture_modules: str) -> i18n.Catalogs:
    return install_manifests(*(parse_module_dir(FIXTURES / name) for name in fixture_modules))


@pytest.fixture(autouse=True)
def catalogs(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(module_status, "read_module_status", lambda *a, **kw: {})
    yield install()
    i18n.install(None)


def user(ui_locale: str | None = None) -> accounts.User:
    return accounts.User(id=7, username="alice", role="user", is_active=True, ui_locale=ui_locale)


def session_body(web) -> dict:
    r = httpx.get(f"{web}/api/session", cookies=COOKIES)
    assert r.status_code == 200
    return r.json()


def patch_locale(web, body, *, csrf: bool = True, if_match: str | None = None):
    headers = panel_headers(**({"X-CSRF-Token": sessions.csrf_token(TOKEN)} if csrf else {}),
                            **({"If-Match": if_match} if if_match is not None else {}))
    return httpx.patch(f"{web}/api/profile/locale", json=body, cookies=COOKIES, headers=headers)


@pytest.mark.parametrize(("configured", "locale", "title"), [
    (None, "en", "Sign in to Agento"),
    ("pl", "pl", None),
    ("de", "en", "Sign in to Agento"),
])
def test_bootstrap_gives_the_instance_language_and_only_the_login_keys(web, monkeypatch, catalogs, configured,
                                                                       locale, title):
    if configured:
        monkeypatch.setenv(ENV, configured)

    r = httpx.get(f"{web}/api/panel/bootstrap")

    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"locale", "version", "messages"}
    assert (body["locale"], body["version"]) == (locale, catalogs.login[locale].version)
    assert set(body["messages"]) == {"panel"}
    assert set(body["messages"]["panel"]) == set(i18n.LOGIN_KEYS)
    assert title is None or body["messages"]["panel"]["login.title"] == title
    assert r.headers.get_list("cache-control") == ["no-store"]


def test_bootstrap_never_carries_a_login_key_that_an_override_adds(web, monkeypatch):
    install("override_first")
    monkeypatch.setenv(ENV, "pl")

    panel = httpx.get(f"{web}/api/panel/bootstrap").json()["messages"]["panel"]

    assert panel["login.title"] == "Wejdź do Agento (first)"
    assert set(panel) == set(i18n.LOGIN_KEYS)


def test_bootstrap_ignores_a_session_and_the_request_language(web, monkeypatch, counted):
    sign_in_as(monkeypatch, user("pl"))

    r = httpx.get(f"{web}/api/panel/bootstrap?locale=pl", cookies=COOKIES, headers={"Accept-Language": "pl"})

    assert r.json()["locale"] == "en"
    assert any(not bucket.private for buckets in counted for bucket in buckets)


@pytest.mark.parametrize(("stored", "configured", "pack", "effective"), [
    (None, None, False, "en"),
    (None, "pl", False, "pl"),
    ("en", "pl", False, "en"),
    ("de", "pl", False, "pl"),
    ("de", "pl", True, "de"),
])
def test_the_session_carries_the_stored_and_the_effective_language(web, monkeypatch, stored, configured, pack,
                                                                 effective):
    snapshot = install("pack_de") if pack else install()
    if configured:
        monkeypatch.setenv(ENV, configured)
    sign_in_as(monkeypatch, user(stored))

    body = session_body(web)

    assert body["user"]["ui_locale"] == stored
    assert body["locale"] == {
        "effective": effective, "instance": configured or "en",
        "available": [{"tag": tag, "name": name} for tag, name in snapshot.languages.items()],
        "version": snapshot.versions[effective],
    }
    assert ({"tag": "de", "name": "Deutsch"} in body["locale"]["available"]) is pack


def test_login_carries_the_same_language_block_as_a_session_read(web, monkeypatch):
    monkeypatch.setenv(ENV, "pl")
    session = sign_in_as(monkeypatch, user("pl"))
    monkeypatch.setattr(sessions, "sign_in", lambda conn, username, password: (session, TOKEN))

    posted = httpx.post(f"{web}/api/session", json={"username": "alice", "password": "correct horse battery"},
                        headers=panel_headers()).json()

    read = session_body(web)
    assert posted["locale"] == read["locale"]
    assert posted["user"]["ui_locale"] == read["user"]["ui_locale"] == "pl"


def test_a_private_text_changes_the_catalog_version_and_nothing_of_the_bootstrap(web, monkeypatch, tmp_path):
    sign_in_as(monkeypatch, user())

    def answers(greeting: str) -> tuple[bytes, str]:
        private = parse_module_dir(write_module(
            tmp_path / greeting, "private_mod", i18n_json={"version": 1, "catalog": {"en": "en.json"}},
            **{"en.json": {"greeting": greeting}}))
        install_manifests(private)
        return (httpx.get(f"{web}/api/panel/bootstrap").content,
                httpx.get(f"{web}/api/panel/i18n/pl", cookies=COOKIES).headers["etag"])

    (bootstrap, etag), (bootstrap_after, etag_after) = answers("Hello"), answers("Welcome")

    assert etag != etag_after
    assert bootstrap == bootstrap_after


def test_the_language_reads_open_no_json_file_after_install(web, monkeypatch, stored):
    session = sign_in_as(monkeypatch, user("pl"))
    monkeypatch.setenv(ENV, "pl")
    monkeypatch.setenv("CONFIG__ADMIN__LOCALE__DATE_FORMAT", "iso")
    monkeypatch.setattr(api, "_DISPLAY", api.display_schema([LANGUAGE_OWNER]))
    monkeypatch.setattr(acl, "_REGISTRY", acl.load([(LANGUAGE_OWNER.name, LANGUAGE_OWNER.path)]))
    read_text, open_path = Path.read_text, Path.open

    def guarded(read):
        def reader(self, *args, **kwargs):
            if self.suffix == ".json" and self.name != "modules.json":
                raise AssertionError("a module file was read after install")
            return read(self, *args, **kwargs)
        return reader

    def no_disk(*_a, **_kw):
        raise AssertionError("a module file was read after install")

    monkeypatch.setattr(Path, "read_text", guarded(read_text))
    monkeypatch.setattr(Path, "open", guarded(open_path))
    monkeypatch.setattr("agento.framework.config_resolver.read_config_defaults", no_disk)
    monkeypatch.setattr(i18n, "load", no_disk)
    monkeypatch.setattr(i18n, "enabled_sources", no_disk)
    monkeypatch.setattr(data, "get_module_schemas", no_disk)
    monkeypatch.setattr("agento.framework.module_loader.parse_module_dir", no_disk)
    monkeypatch.setattr("agento.framework.harness.resolve_options", no_disk)
    monkeypatch.setattr(sessions, "sign_in", lambda conn, username, password: (session, TOKEN))

    for _ in range(3):
        assert httpx.post(f"{web}/api/session", json={"username": "alice", "password": "correct horse battery"},
                          headers=panel_headers()).json()["locale"]["effective"] == "pl"
        assert session_body(web)["locale"] == {**session_body(web)["locale"], "effective": "pl", "instance": "pl"}
        assert session_body(web)["display"] == {"date_format": "iso", "timezone": "browser",
                                                "date_format_explicit": True}
        assert httpx.get(f"{web}/api/panel/i18n/pl", cookies=COOKIES).status_code == 200
        assert httpx.get(f"{web}/api/panel/bootstrap").json()["locale"] == "pl"
        assert patch_locale(web, {"ui_locale": "de"}).json()["code"] == "LOCALE_UNSUPPORTED"
        assert patch_locale(web, {"ui_locale": "en"}).json()["locale"]["effective"] == "en"


class Unread(dict):
    def _refuse(self, *_a, **_kw):
        raise AssertionError("a namespace was read after install")

    __getitem__ = __iter__ = __contains__ = get = keys = values = items = _refuse


def install_without_namespaces(*fixture_modules: str) -> i18n.Catalogs:
    served = install(*fixture_modules)
    i18n.install(dataclasses.replace(served, messages=Unread(), fallback=Unread()))
    return served


@pytest.mark.parametrize(("configured", "locale"), [
    (None, "en"), ("en", "en"), ("pl", "pl"), ("de", "de"), ("fr", "en"),
])
def test_bootstrap_answers_from_the_start_snapshot_and_merges_no_namespace(web, monkeypatch, configured, locale):
    served = install_without_namespaces("pack_de")
    if configured:
        monkeypatch.setenv(ENV, configured)

    def no_merge(*_a, **_kw):
        raise AssertionError("a namespace was merged after install")

    monkeypatch.setattr(i18n, "_login", no_merge)

    for _ in range(2):
        r = httpx.get(f"{web}/api/panel/bootstrap")
        assert r.status_code == 200
        assert r.json() == {"locale": locale, "version": served.login[locale].version,
                            "messages": {"panel": served.login[locale].messages}}


def test_the_answers_that_carry_no_catalog_read_no_namespace(web, monkeypatch, stored):
    served = install_without_namespaces("pack_de")
    session = sign_in_as(monkeypatch, user("de"))
    monkeypatch.setattr(sessions, "sign_in", lambda conn, username, password: (session, TOKEN))
    etag = f'"{served.versions["pl"]}"'

    posted = httpx.post(f"{web}/api/session", json={"username": "alice", "password": "correct horse battery"},
                        headers=panel_headers())
    revalidated = httpx.get(f"{web}/api/panel/i18n/pl", cookies=COOKIES, headers={"If-None-Match": etag})

    assert posted.json()["locale"] == session_body(web)["locale"]
    assert session_body(web)["locale"]["version"] == served.versions["de"]
    assert (revalidated.status_code, revalidated.content) == (304, b"")
    assert patch_locale(web, {"ui_locale": "pl"}, if_match=etag).status_code == 200
    assert patch_locale(web, {"ui_locale": "pl"}, if_match='"0000"').json()["code"] == "CATALOG_CHANGED"
    with pytest.raises(AssertionError):
        i18n.current().messages["pl"]


def test_the_catalog_needs_a_session(web):
    r = httpx.get(f"{web}/api/panel/i18n/pl")

    assert r.status_code == 401
    assert r.json() == {"error": "not signed in", "code": "AUTH_REQUIRED"}


def test_the_catalog_of_a_tag_the_registry_lacks_is_404(web, monkeypatch):
    sign_in_as(monkeypatch, user())

    for tag in ("de", "xx-YY"):
        r = httpx.get(f"{web}/api/panel/i18n/{tag}", cookies=COOKIES)
        assert (r.status_code, r.json()["code"]) == (404, "NOT_FOUND")
        assert r.headers.get_list("cache-control") == ["no-store"]


def test_the_catalog_is_revalidated_by_its_version(web, monkeypatch, catalogs):
    sign_in_as(monkeypatch, user())

    r = httpx.get(f"{web}/api/panel/i18n/pl", cookies=COOKIES)

    assert r.status_code == 200
    assert r.json() == {"locale": "pl", "version": catalogs.versions["pl"], "messages": catalogs.messages["pl"],
                        "fallback": catalogs.fallback["pl"]}
    assert r.headers.get_list("cache-control") == ["private, max-age=0"]
    assert r.headers["etag"] == f'"{catalogs.versions["pl"]}"'
    assert r.headers["x-content-type-options"] == "nosniff"

    again = httpx.get(f"{web}/api/panel/i18n/pl", cookies=COOKIES, headers={"If-None-Match": r.headers["etag"]})
    assert (again.status_code, again.content) == (304, b"")
    assert "content-length" not in again.headers and "transfer-encoding" not in again.headers
    assert again.headers.get_list("cache-control") == ["private, max-age=0"]
    stale = httpx.get(f"{web}/api/panel/i18n/pl", cookies=COOKIES, headers={"If-None-Match": '"0000"'})
    assert stale.status_code == 200


def test_a_key_that_a_pack_lacks_is_served_as_the_english_fallback(web, monkeypatch):
    snapshot = install("pack_de")
    sign_in_as(monkeypatch, user())

    body = httpx.get(f"{web}/api/panel/i18n/de", cookies=COOKIES).json()

    assert body["messages"]["panel"]["login.title"] == "Bei Agento anmelden"
    assert "login.password" not in body["messages"]["panel"]
    assert body["fallback"]["panel"]["login.password"] == snapshot.messages["en"]["panel"]["login.password"]


@pytest.fixture
def stored(monkeypatch):
    calls = MagicMock()
    monkeypatch.setattr(accounts, "set_ui_locale", calls)
    return calls


def test_the_profile_language_is_stored_for_the_session_account(web, monkeypatch, stored, catalogs):
    monkeypatch.setenv(ENV, "en")
    sign_in_as(monkeypatch, user("en"))

    r = patch_locale(web, {"ui_locale": "pl"})

    assert r.status_code == 200
    assert stored.call_args.args[1:] == (7, "pl")
    assert r.json() == {"ui_locale": "pl", "locale": {
        "effective": "pl", "instance": "en",
        "available": [{"tag": "en", "name": "English"}, {"tag": "pl", "name": "Polski"}],
        "version": catalogs.versions["pl"]}}


def test_a_language_save_is_refused_when_the_catalog_is_not_the_one_the_caller_holds(web, monkeypatch, stored,
                                                                                    tmp_path):
    sign_in_as(monkeypatch, user("en"))
    held = httpx.get(f"{web}/api/panel/i18n/pl", cookies=COOKIES).headers["ETag"]

    same = patch_locale(web, {"ui_locale": "pl"}, if_match=held)

    assert (same.status_code, f'"{same.json()["locale"]["version"]}"') == (200, held)
    assert stored.call_count == 1
    for malformed in (held.strip('"'), f"W/{held}", f"{held}, {held}", "*", ""):
        refused = patch_locale(web, {"ui_locale": "pl"}, if_match=malformed)
        assert (refused.status_code, refused.json()["code"]) == (409, "CATALOG_CHANGED")
    assert stored.call_count == 1

    install_manifests(parse_module_dir(write_module(
        tmp_path, "restarted", i18n_json={"version": 1, "catalog": {"en": "en.json"}},
        **{"en.json": {"greeting": "Hello"}})))
    changed = patch_locale(web, {"ui_locale": "pl"}, if_match=held)

    assert (changed.status_code, changed.json()) == (409, {"error": "the catalog changed", "code": "CATALOG_CHANGED"})
    assert stored.call_count == 1
    assert patch_locale(web, {"ui_locale": "pl"}).status_code == 200


@pytest.mark.parametrize(("body", "code", "params"), [
    ({"ui_locale": "de"}, "LOCALE_UNSUPPORTED", {"ui_locale": "de"}),
    ({"ui_locale": "pl", "version": "0000"}, "INVALID_REQUEST", None),
    ({"ui_locale": "de", "version": "0000"}, "INVALID_REQUEST", None),
    ({"ui_locale": "pl", "user_id": 1}, "INVALID_REQUEST", None),
    ({"ui_locale": "pl", "id": 1}, "INVALID_REQUEST", None),
    ({"ui_locale": None}, "INVALID_REQUEST", None),
    ({}, "INVALID_REQUEST", None),
    (["pl"], "INVALID_REQUEST", None),
])
def test_a_profile_language_body_that_is_not_exact_is_refused(web, monkeypatch, stored, body, code, params):
    sign_in_as(monkeypatch, user("en"))

    r = patch_locale(web, body)

    assert r.status_code == 400
    assert r.json()["code"] == code
    assert r.json().get("params") == params
    stored.assert_not_called()


def test_the_profile_language_write_needs_the_csrf_token_and_a_session(web, monkeypatch, stored):
    monkeypatch.setattr(sessions, "lookup_session", lambda conn, token: None)
    assert patch_locale(web, {"ui_locale": "pl"}).status_code == 401
    sign_in_as(monkeypatch, user("en"))

    r = patch_locale(web, {"ui_locale": "pl"}, csrf=False)

    assert (r.status_code, r.json()) == (403, {"error": "forbidden", "code": "FORBIDDEN"})
    stored.assert_not_called()
