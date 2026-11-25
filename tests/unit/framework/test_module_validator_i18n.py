"""``module:validate`` rules for a module's ``i18n.json`` and the catalog files it names."""
from __future__ import annotations

from pathlib import Path

import pytest

from agento.framework import i18n
from agento.framework.module_loader import scan_modules
from agento.framework.module_validator import validate_all, validate_module
from tests.module_writer import write_module

FIXTURES = Path(__file__).parents[2] / "fixtures" / "i18n"
DECLARATION = {"version": 1, "catalog": {"en": "i18n/en.json", "pl": "i18n/pl.json"}}
ENGLISH = {"greeting": "Hello {name}", "jobs": {"one": "{count} job", "other": "{count} jobs"}}


def i18n_errors(module_dir: Path) -> list[str]:
    return [e for e in validate_module(module_dir) if e.startswith("i18n.json")]


def demo(root: Path, declaration: object = DECLARATION, name: str = "demo", **files: object) -> Path:
    catalogs = {"i18n/en.json": ENGLISH, "i18n/pl.json": {"greeting": "Cześć {name}"}}
    return write_module(root, name, i18n_json=declaration, **{**catalogs, **files})


def test_a_partial_catalog_is_valid(tmp_path):
    assert i18n_errors(demo(tmp_path)) == []


@pytest.mark.parametrize("fixture", ["pack_de", "override_first", "override_second"])
def test_the_fixture_modules_are_valid(fixture):
    assert validate_module(FIXTURES / fixture) == []


@pytest.mark.parametrize(("declaration", "why"), [
    ("{", "invalid JSON"),
    ([], "must be an object"),
    ({"catalog": {}}, "'version' must be 1"),
    ({"version": 1, "extra": 1}, "unknown key 'extra'"),
    ({"version": 1, "languages": {}}, "'languages' must be a list"),
    ({"version": 1, "languages": [{"tag": "de"}]}, "must be {tag, name}"),
    ({"version": 1, "languages": [{"tag": "DE", "name": "Deutsch"}]}, "language tag 'DE' must match"),
    ({"version": 1, "languages": [{"tag": "de_DE", "name": "Deutsch"}]}, "language tag 'de_DE' must match"),
    ({"version": 1, "catalog": []}, "'catalog' must be an object"),
    ({"version": 1, "packs": {"german": "i18n/pl.json"}}, "packs language tag 'german' must match"),
    ({"version": 1, "catalog": {"pl": "../pl.json"}}, "must be relative, stay inside the module directory"),
    ({"version": 1, "catalog": {"pl": "/tmp/pl.json"}}, "must be relative, stay inside the module directory"),
    ({"version": 1, "catalog": {"pl": "i18n/pl.txt"}}, "end in .json"),
    ({"version": 1, "catalog": {"pl": "i18n/none.json"}}, "file 'i18n/none.json' not found"),
    ({"version": 1, "catalog": {"pl": "i18n/broken.json"}}, "must be a JSON object"),
    ({"version": 1, "packs": {"pl": "i18n/pl.json"}}, "namespace 'greeting' is not a known module"),
])
def test_a_declaration_that_is_not_valid_is_refused(tmp_path, declaration, why):
    errors = i18n_errors(demo(tmp_path, declaration, **{"i18n/broken.json": "{"}))

    assert any(why in e for e in errors), errors


@pytest.mark.parametrize(("polish", "why"), [
    ({"greeting": "Cześć {nick}"}, "demo:greeting: unknown param 'nick'"),
    ({"jobs": {"one": "{count} zadanie"}}, "demo:jobs: a plural message needs 'other'"),
    ({"jobs": {"other": "{count} zadań", "lots": "x"}}, "demo:jobs: a plural message maps"),
    ({"greeting": 5}, "demo:greeting: a message is text or a plural object"),
])
def test_a_message_that_is_not_valid_is_refused(tmp_path, polish, why):
    errors = i18n_errors(demo(tmp_path, **{"i18n/pl.json": polish}))

    assert any(why in e for e in errors), errors


def test_a_translated_key_with_no_english_source_takes_no_param(tmp_path):
    assert i18n_errors(demo(tmp_path, **{"i18n/pl.json": {"only_polish": "Tylko tu"}})) == []
    assert i18n_errors(demo(tmp_path / "second", **{"i18n/pl.json": {"only_polish": "Tylko {tu}"}})) == [
        "i18n.json: catalog.pl: demo:only_polish: unknown param 'tu'"]


@pytest.mark.parametrize("name", ["ui", "panel"])
def test_a_module_may_not_take_a_built_in_namespace(tmp_path, name):
    errors = i18n_errors(demo(tmp_path, name=name))

    assert len(errors) == 1 and "belongs to the framework" in errors[0]


def test_a_pack_may_target_another_known_module_and_is_checked_against_its_english(tmp_path):
    demo(tmp_path)
    write_module(tmp_path, "demo_de", i18n_json={"version": 1, "languages": [{"tag": "de", "name": "Deutsch"}],
                                                 "packs": {"de": "de.json"}},
                 **{"de.json": {"demo": {"greeting": "Hallo {vorname}"}, "panel": {"login.title": "Anmelden"}}})

    errors = validate_all(tmp_path, tmp_path / "none")

    assert errors == {"demo_de": ["i18n.json: packs.de: demo:greeting: unknown param 'vorname'"]}


def named(root: Path, directory: str, name: str, **files: object) -> Path:
    return write_module(root, directory, module_json={"name": name}, **files)


@pytest.mark.parametrize("reserved", ["ui", "panel"])
def test_a_reserved_namespace_is_refused_by_the_manifest_name_whatever_the_directory_is(tmp_path, reserved):
    module_dir = named(tmp_path, "demo", reserved, i18n_json=DECLARATION,
                       **{"i18n/en.json": ENGLISH, "i18n/pl.json": {"greeting": "Cześć {name}"}})

    errors = i18n_errors(module_dir)

    assert len(errors) == 1 and "belongs to the framework" in errors[0]
    assert load_from(tmp_path).languages == load_from(tmp_path / "none").languages


def load_from(root: Path) -> i18n.Catalogs:
    return i18n.load(i18n.ordered_sources(scan_modules(str(root)) if root.is_dir() else []))


def test_the_namespace_of_a_module_is_its_manifest_name_in_the_validator_the_loader_and_the_derived_keys(tmp_path):
    named(tmp_path, "demo_dir", "demo", i18n_json=DECLARATION, system_json={"mode": {"label": "Mode"}},
          **{"i18n/en.json": ENGLISH, "i18n/pl.json": {"greeting": "Cześć {name}", "module.title": "Pokaz"}})
    named(tmp_path, "pack_dir", "pack", i18n_json={"version": 1, "packs": {"pl": "pl.json"}},
          **{"pl.json": {"demo": {"greeting": "Hej {name}"}}})
    named(tmp_path, "wrong_dir", "wrong", i18n_json={"version": 1, "packs": {"pl": "pl.json"}},
          **{"pl.json": {"demo_dir": {"greeting": "Hej {name}"}, "demo": {"greeting": "Hej {imie}"}}})

    served = load_from(tmp_path)

    assert validate_all(tmp_path, tmp_path / "none") == {"wrong_dir": [
        "i18n.json: packs.pl: namespace 'demo_dir' is not a known module, 'ui' or 'panel'",
        "i18n.json: packs.pl: demo:greeting: unknown param 'imie'"]}
    assert "demo_dir" not in served.messages["en"] and "demo_dir" not in served.messages["pl"]
    assert served.messages["en"]["demo"]["module.title"] == "Demo"
    assert served.messages["pl"]["demo"] == {"greeting": "Hej {name}", "module.title": "Pokaz"}


def test_a_manifest_linked_from_outside_is_valid_as_on_the_base_and_gives_no_name_to_the_loader(tmp_path, caplog):
    module_dir = write_module(tmp_path / "code", "demo", i18n_json=DECLARATION,
                              **{"i18n/en.json": ENGLISH, "i18n/pl.json": {"greeting": "Cześć {name}"}})
    outside = tmp_path / "outside.json"
    outside.write_text('{"name": "panel"}')
    (module_dir / "module.json").unlink()
    (module_dir / "module.json").symlink_to(outside)

    with caplog.at_level("WARNING"):
        served = load_from(tmp_path / "code")

    assert i18n.module_errors(module_dir, [module_dir]) == []
    assert served.messages["pl"]["demo"] == {"greeting": "Cześć {name}"}
    assert [r.getMessage() for r in caplog.records] == ["i18n skip: module=demo file=module.json reason=unusable file"]
