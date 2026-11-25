"""The shipped catalogs: Polish covers every text a manifest gives, and every file ships in the wheel."""
from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

import pytest

from agento.framework import i18n
from agento.framework.access import acl
from agento.framework.bootstrap import CORE_MODULES_DIR
from agento.framework.module_loader import parse_module_dir, scan_modules
from tests.module_writer import write_module

ROOT = Path(__file__).parents[3]
MANIFESTS = scan_modules(CORE_MODULES_DIR)
CATALOGS = i18n.load(i18n.ordered_sources(MANIFESTS))
DERIVED = {m.name: i18n.manifest_messages(m.path) for m in MANIFESTS}
DERIVED[i18n.PANEL_NAMESPACE] = i18n.manifest_messages(acl.CORE_FILE.parent)
WITH_TEXT = sorted(name for name, messages in DERIVED.items() if messages)


def test_the_scan_finds_the_modules_with_manifest_text():
    assert {"admin", "core", "conversation", "scheduler", "versioned_artifacts", "panel"} <= set(WITH_TEXT)
    assert "config.locale/language.label" in DERIVED["admin"]
    assert "acl.admin.users.create" in DERIVED["panel"]


@pytest.mark.parametrize("namespace", WITH_TEXT)
def test_polish_has_every_key_that_a_manifest_gives(namespace):
    polish = CATALOGS.messages["pl"].get(namespace, {})

    assert sorted(set(DERIVED[namespace]) - set(polish)) == []


def keys_that_polish_lacks(catalogs: i18n.Catalogs) -> dict[str, list[str]]:
    """Per namespace, the English keys with no entry in the composed Polish messages. The
    fallback map is not read: a key that only falls back to English is a key Polish lacks."""
    lacking = {namespace: sorted(set(english) - set(catalogs.messages["pl"].get(namespace, {})))
               for namespace, english in catalogs.messages["en"].items()}
    return {namespace: keys for namespace, keys in lacking.items() if keys}


def test_the_served_polish_catalog_has_every_english_key_of_every_shipped_namespace():
    shipped = {path.parent.name for path in Path(i18n.__file__).parent.glob("*/pl.json")}
    shipped |= {path.parents[1].name for path in Path(CORE_MODULES_DIR).glob("*/i18n/pl.json")}
    assert {"ui", "panel", "admin"} <= shipped <= set(CATALOGS.messages["en"])
    assert keys_that_polish_lacks(CATALOGS) == {}


def test_the_served_key_check_fails_when_a_catalog_file_is_no_longer_declared(tmp_path):
    files = {"i18n/en.json": {"greeting": "Hello"}, "i18n/pl.json": {"greeting": "Cześć"}}
    declared = write_module(tmp_path / "declared", "demo", i18n_json={
        "version": 1, "catalog": {"en": "i18n/en.json", "pl": "i18n/pl.json"}}, **files)
    dropped = write_module(tmp_path / "dropped", "demo", i18n_json={
        "version": 1, "catalog": {"en": "i18n/en.json"}}, **files)

    def lacking(module_dir: Path) -> dict[str, list[str]]:
        return keys_that_polish_lacks(i18n.load(i18n.ordered_sources([parse_module_dir(module_dir)])))

    assert lacking(declared) == {}
    assert lacking(dropped) == {"demo": ["greeting"]}


def params_that_differ(english: dict, polish: dict) -> dict[str, list[str]]:
    """Per key that both languages have, the params that only one of the two texts names."""
    differing = {key: sorted(i18n.holes(polish[key]) ^ i18n.holes(text))
                 for key, text in english.items() if key in polish}
    return {key: params for key, params in differing.items() if params}


DESCRIPTOR_KEYS = [("conversation", "nav./conversations"), ("conversation", "joblink"),
                   ("scheduler", "nav./schedules"), ("miniapps", "nav./apps"),
                   ("versioned_artifacts", "nav./artifacts"), ("claude", "limit.Week"), ("codex", "limit.Week")]


def test_the_descriptor_keys_are_the_polish_keys_with_no_english_source_in_json():
    without_source = {(namespace, key) for namespace, entries in CATALOGS.messages["pl"].items()
                      for key in entries if key not in CATALOGS.messages["en"].get(namespace, {})}

    assert without_source == set(DESCRIPTOR_KEYS)


@pytest.mark.parametrize("name, key", DESCRIPTOR_KEYS)
def test_a_translation_of_a_key_with_no_english_source_in_json_takes_no_param(tmp_path, name, key):
    module_dir = shutil.copytree(Path(CORE_MODULES_DIR) / name, tmp_path / name)
    polish = module_dir / "i18n" / "pl.json"
    messages = json.loads(polish.read_text(encoding="utf-8"))
    polish.write_text(json.dumps({**messages, key: "Text {unused}"}), encoding="utf-8")

    served = i18n.load(i18n.sources_for([(name, module_dir)])).messages["pl"][name]

    assert i18n.module_errors(module_dir, [module_dir]) == [
        f"i18n.json: catalog.pl: {name}:{key}: unknown param 'unused'"]
    assert set(served) == set(messages) - {key}


@pytest.mark.parametrize("namespace", sorted(CATALOGS.messages["en"]))
def test_a_shipped_polish_text_has_exactly_the_params_of_its_english_text(namespace):
    assert params_that_differ(CATALOGS.messages["en"][namespace], CATALOGS.messages["pl"].get(namespace, {})) == {}


@pytest.mark.parametrize(("polish", "differing"), [
    ("Hasło ma mieć od {min} znaków.", ["max"]),
    ("Hasło ma mieć od {min} do {max} znaków ({unit}).", ["unit"]),
    ({"one": "{count} znak", "other": "znaki"}, ["count", "max", "min"]),
    ("Hasło ma mieć od {min} do {max} znaków.", None),
])
def test_the_param_check_refuses_a_translation_that_drops_or_adds_a_param(polish, differing):
    english = {"password": "The password must have {min} to {max} characters.", "english_only": "{count}"}

    assert params_that_differ(english, {"password": polish, "polish_only": "{x}"}).get("password") == differing


SOURCES = {source.name: source for source in i18n.ordered_sources(MANIFESTS)}


@pytest.mark.parametrize("namespace", WITH_TEXT)
def test_no_english_catalog_entry_repeats_a_manifest_text(namespace):
    source = SOURCES[namespace]
    declaration = source.directory / i18n.FILE_NAME
    relative = json.loads(declaration.read_text()).get("catalog", {}).get("en") if declaration.is_file() else None
    declared = json.loads((source.directory / relative).read_text()) if relative else {}

    assert sorted(set(declared) & set(DERIVED[namespace])) == []


def test_every_shipped_catalog_declaration_is_valid():
    dirs = [m.path for m in MANIFESTS]

    problems = {m.name: found for m in MANIFESTS if (found := i18n.module_errors(m.path, dirs))}

    assert problems == {}


def test_the_built_in_catalog_files_are_in_the_wheel_include_set():
    include = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["include"]
    declared = [path for source in i18n.ordered_sources(MANIFESTS)
                for path in (source.directory / i18n.FILE_NAME, *source.directory.glob("i18n/*.json"),
                             *source.directory.glob("*.json"))
                if path.is_file()]

    assert "src/agento/**/*.json" in include
    assert (i18n.BUILTIN_DIR / "panel" / "pl.json") in declared
    assert all(path.suffix == ".json" and (ROOT / "src" / "agento") in path.parents for path in declared)


def test_a_shipped_module_has_one_name_for_its_directory_its_manifest_and_its_panel_descriptor():
    import re

    for manifest in MANIFESTS:
        assert manifest.name == manifest.path.name == i18n.module_name(manifest.path)
        descriptor = manifest.path / "panel" / "index.ts"
        if descriptor.is_file():
            assert re.findall(r'^\s*id: "([^"]+)",$', descriptor.read_text(), re.M) == [manifest.name]
