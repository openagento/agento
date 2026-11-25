"""The panel catalog loader: registry, layers, skip rule, versions, language choice."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agento.framework import i18n
from agento.framework.bootstrap import CORE_MODULES_DIR
from agento.framework.module_loader import parse_module_dir
from tests.module_writer import write_module

FIXTURES = Path(__file__).parents[2] / "fixtures" / "i18n"
REGISTRY = {"en": "English", "pl": "Polski"}
LANGUAGE_OWNER = parse_module_dir(Path(CORE_MODULES_DIR) / "admin")
ENV = "CONFIG__ADMIN__LOCALE__LANGUAGE"
MARKER = "marker value 7f3a"
LONE_SURROGATE = chr(0xD800)


def fixture_manifests(*names: str) -> list:
    return [parse_module_dir(FIXTURES / name) for name in names]


def load_with(*manifests) -> i18n.Catalogs:
    return i18n.load(i18n.ordered_sources(manifests))


def module_with_catalog(root: Path, name: str, **languages: dict):
    files = {f"i18n/{tag}.json": messages for tag, messages in languages.items()}
    declaration = {"version": 1, "catalog": {tag: f"i18n/{tag}.json" for tag in languages}}
    return parse_module_dir(write_module(root, name, i18n_json=declaration, **files))


def test_the_built_in_sources_register_english_and_polish_with_every_login_key():
    catalogs = load_with()

    assert catalogs.languages == REGISTRY
    for tag in REGISTRY:
        assert set(catalogs.login[tag].messages) == set(i18n.LOGIN_KEYS)
    assert catalogs.fallback["en"] == {}


def test_a_pack_registers_its_language_and_a_key_it_lacks_answers_english():
    catalogs = load_with(*fixture_manifests("pack_de"))

    assert catalogs.languages["de"] == "Deutsch"
    assert catalogs.messages["de"]["panel"]["login.title"] == "Bei Agento anmelden"
    assert "login.password" not in catalogs.messages["de"]["panel"]
    assert catalogs.fallback["de"]["panel"]["login.password"] == catalogs.messages["en"]["panel"]["login.password"]
    assert "login.title" not in catalogs.fallback["de"]["panel"]


def test_an_override_beats_a_pack_and_a_pack_beats_the_source(tmp_path):
    source = module_with_catalog(tmp_path, "demo", en={"greeting": "Hello"}, pl={"greeting": "source"})
    pack = parse_module_dir(write_module(
        tmp_path, "demo_pack", i18n_json={"version": 1, "packs": {"pl": "pack.json"}},
        **{"pack.json": {"demo": {"greeting": "pack", "farewell": "pack only"}}}))
    override = parse_module_dir(write_module(
        tmp_path, "a_override", i18n_json={"version": 1, "overrides": {"pl": "override.json"}},
        **{"override.json": {"demo": {"greeting": "override"}}}))

    assert load_with(source).messages["pl"]["demo"]["greeting"] == "source"
    assert load_with(source, pack).messages["pl"]["demo"]["greeting"] == "pack"
    layered = load_with(source, pack, override).messages["pl"]["demo"]
    assert layered == {"greeting": "override", "farewell": "pack only"}


@pytest.mark.parametrize(("bad", "reason"), [
    ({"one": "a job"}, "needs 'other'"),
    ({"other": "jobs", "plenty": "jobs"}, "plural message maps"),
    ({"other": 3}, "plural message maps"),
    (7, "text or a plural object"),
    ("Witaj {nick}", "unknown param 'nick'"),
])
def test_an_entry_that_is_not_valid_is_skipped_with_a_log_line_and_the_lower_layer_stays(tmp_path, caplog, bad, reason):
    source = module_with_catalog(tmp_path, "demo", en={"greeting": "Hello {name}"}, pl={"greeting": "Cześć {name}"})
    override = parse_module_dir(write_module(
        tmp_path, "tweak", i18n_json={"version": 1, "overrides": {"pl": "override.json"}},
        **{"override.json": {"demo": {"greeting": bad}}}))

    with caplog.at_level(logging.WARNING, logger="agento.framework.i18n"):
        catalogs = load_with(source, override)

    assert catalogs.messages["pl"]["demo"]["greeting"] == "Cześć {name}"
    skipped = [r.getMessage() for r in caplog.records if r.getMessage().startswith("i18n skip")]
    assert len(skipped) == 1
    assert "module=tweak" in skipped[0] and "key=greeting" in skipped[0] and reason in skipped[0]


@pytest.mark.parametrize("declaration", [
    {"version": 1, "catalog": {"pl": "../outside.json"}},
    {"version": 1, "catalog": {"pl": "missing.json"}},
    {"version": 1, "catalog": {"pl": "notes.txt"}},
    {"version": 2, "catalog": {"pl": "pl.json"}},
    "not an object",
])
def test_a_file_that_cannot_be_used_never_stops_the_load(tmp_path, declaration):
    (tmp_path / "outside.json").write_text('{"greeting": "escaped"}')
    module = parse_module_dir(write_module(tmp_path, "demo", i18n_json=declaration,
                                           **{"pl.json": {"greeting": "ok"}, "notes.txt": "{}"}))

    assert "demo" not in load_with(module).messages["pl"]


@pytest.mark.parametrize(("relative", "inside"), [
    ("i18n/pl.json", True),
    ("../pl.json", False),
    ("/etc/passwd.json", False),
    ("i18n/../../pl.json", False),
    ("i18n/pl.yaml", False),
    (5, False),
])
def test_a_catalog_path_stays_inside_the_module_directory(tmp_path, relative, inside):
    assert (i18n.catalog_file(tmp_path, relative) is not None) is inside


def test_the_version_is_stable_and_follows_the_english_fallback_of_a_partial_language(tmp_path):
    def versions(root: str, greeting: str) -> dict[str, str]:
        english = module_with_catalog(tmp_path / root, "demo", en={"greeting": greeting, "farewell": "Bye"})
        return load_with(english, *fixture_manifests("pack_de")).versions

    first, again, changed = versions("a", "Hello"), versions("b", "Hello"), versions("c", "Good day")

    assert first == again
    assert all(len(v) == 16 for v in first.values())
    assert changed["de"] != first["de"] and changed["en"] != first["en"]


def test_the_winner_of_a_conflict_is_the_last_module_in_resolve_order_and_is_logged(caplog):
    manifests = fixture_manifests("override_first", "override_second")
    titles = set()
    for seed in range(6):
        shuffled = manifests[:]
        random.Random(seed).shuffle(shuffled)
        with caplog.at_level(logging.WARNING, logger="agento.framework.i18n"):
            titles.add(load_with(*shuffled).messages["pl"]["panel"]["login.title"])

    assert titles == {"Wejdź do Agento (second)"}
    conflict = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("i18n conflict"))
    assert "language=pl namespace=panel key=login.title" in conflict
    assert "modules=override_first,override_second winner=override_second" in conflict


def test_with_the_winner_disabled_the_other_override_returns():
    only_first = load_with(*fixture_manifests("override_first"))

    assert only_first.messages["pl"]["panel"]["login.title"] == "Wejdź do Agento (first)"


def test_an_override_cannot_add_a_public_login_key():
    catalogs = load_with(*fixture_manifests("override_first"))

    assert catalogs.messages["pl"]["panel"]["login.hint"]
    assert "login.hint" not in catalogs.login["pl"].messages


def test_manifest_text_is_the_english_source_of_its_derived_keys(tmp_path):
    module_dir = write_module(
        tmp_path, "demo_mod",
        system_json={"area/mode": {"type": "select", "label": "Mode", "description": "How it runs.",
                                   "options": [{"value": "fast", "label": "Fast"}]},
                     "plain": {"type": "string"}},
        acl_json={"screens": [{"title": "Demo", "leaves": [{"id": "demo_mod", "title": "View"},
                                                           {"id": "demo_mod.edit", "title": "Edit"}]}],
                  "operations": [{"id": "demo_mod.run", "title": "Run it"}]})

    assert i18n.manifest_messages(module_dir) == {
        "module.title": "Demo Mod",
        "config.area/mode.label": "Mode",
        "config.area/mode.description": "How it runs.",
        "config.area/mode.option.fast": "Fast",
        "acl.screen.demo_mod": "Demo",
        "acl.demo_mod": "View",
        "acl.demo_mod.edit": "Edit",
        "acl.demo_mod.run": "Run it",
    }
    assert load_with(parse_module_dir(module_dir)).messages["en"]["demo_mod"]["config.area/mode.label"] == "Mode"


def test_the_framework_acl_titles_are_panel_messages():
    english = load_with().messages["en"]["panel"]

    assert english["acl.screen.admin.dashboard"] == "Dashboard"
    assert english["acl.admin.users.create"] == "Create"
    assert english["acl.artifact.launch"] == "Launch a miniapp"


@pytest.mark.parametrize(("stored", "instance", "expected"), [
    ("pl", "en", "pl"),
    (None, "pl", "pl"),
    ("de", "pl", "pl"),
    ("de", "de", "en"),
    (None, "xx", "en"),
])
def test_effective_language(stored, instance, expected):
    assert i18n.effective(stored, instance, REGISTRY) == expected


def extension(root: Path, name: str, declaration: object, **files: object):
    return parse_module_dir(write_module(root, name, i18n_json=declaration, **files))


def pack(root: Path, content: object, name: str = "tweak"):
    return extension(root, name, {"version": 1, "packs": {"pl": "pack.json"}}, **{"pack.json": content})


CONTENT_POSITIONS = [
    pytest.param({"version": 1, MARKER: MARKER}, {}, id="unknown-declaration-key"),
    pytest.param({"version": 1, "languages": [{"tag": "de", "name": "Deutsch", "note": MARKER}]}, {},
                 id="unexpected-language-field"),
    pytest.param({"version": 1, "languages": [{"tag": MARKER, "name": "Deutsch"}]}, {}, id="language-tag"),
    pytest.param({"version": 1, "catalog": {MARKER: "pl.json"}}, {}, id="layer-language-tag"),
    pytest.param({"version": 1, "catalog": {"pl": f"{MARKER}.json"}}, {}, id="file-path"),
    pytest.param({"version": 1, "packs": {"pl": "pack.json"}}, {"pack.json": {MARKER: {"key": "text"}}},
                 id="namespace"),
    pytest.param({"version": 1, "packs": {"pl": "pack.json"}}, {"pack.json": {"panel": [MARKER]}},
                 id="namespace-content"),
    pytest.param({"version": 1, "packs": {"pl": "pack.json"}}, {"pack.json": {"panel": {MARKER: 5}}}, id="key"),
    pytest.param({"version": 1, "packs": {"pl": "pack.json"}},
                 {"pack.json": {"panel": {"login.title": {"one": MARKER}}}}, id="message-text"),
    pytest.param({"version": 1, "packs": {"pl": "pack.json"}},
                 {"pack.json": {"panel": {"login.title": f"{{{'x' * 200}}}"}}}, id="long-param-name"),
]


@pytest.mark.parametrize(("declaration", "files"), CONTENT_POSITIONS)
def test_no_log_line_and_no_validation_message_quotes_content_that_is_not_an_identifier(
        tmp_path, caplog, declaration, files):
    module = extension(tmp_path, "noisy", declaration, **files)

    with caplog.at_level(logging.DEBUG, logger="agento.framework.i18n"):
        load_with(module)
    refused = i18n.module_errors(module.path, [module.path])

    logged = [record.getMessage() for record in caplog.records]
    assert any(line.startswith("i18n skip: module=noisy") for line in logged)
    assert refused
    for text in (*logged, *refused):
        assert MARKER not in text
        assert "x" * 121 not in text


def test_a_declaration_that_is_refused_is_logged_by_position_and_structural_reason(tmp_path, caplog):
    module = extension(tmp_path, "noisy", {"version": 1, "languages": [
        {"tag": "de", "name": "Deutsch"}, {"tag": "fr", "name": "Français", "note": MARKER}]})

    with caplog.at_level(logging.WARNING, logger="agento.framework.i18n"):
        load_with(module)

    assert [record.getMessage() for record in caplog.records] == [
        "i18n skip: module=noisy file=i18n.json reason=languages[1] is not {tag, name}"]


def nested_too_deep() -> str:
    return "[" * 100_000


def looping_link(module_dir: Path) -> None:
    (module_dir / "loop.json").symlink_to("loop.json")


FILES_THAT_BREAK_A_READER = [
    pytest.param({"version": 1, "catalog": {"pl": chr(0) + ".json"}}, {}, None, id="nul-in-a-path"),
    pytest.param({"version": 1, "catalog": {"pl": "pl.json"}}, {"pl.json": {"greeting": LONE_SURROGATE}}, None,
                 id="lone-surrogate-in-a-message"),
    pytest.param({"version": 1, "catalog": {"pl": "pl.json"}}, {"pl.json": {"plural": {"other": LONE_SURROGATE}}},
                 None, id="lone-surrogate-in-a-plural"),
    pytest.param({"version": 1, "catalog": {"pl": "pl.json"}}, {"pl.json": {LONE_SURROGATE: "text"}}, None,
                 id="lone-surrogate-in-a-key"),
    pytest.param({"version": 1, "catalog": {"en": "en.json"}}, {"en.json": {LONE_SURROGATE: LONE_SURROGATE}}, None,
                 id="lone-surrogate-in-the-english-source"),
    pytest.param({"version": 1, "packs": {"pl": "pack.json"}}, {"pack.json": {LONE_SURROGATE: {"key": "text"}}},
                 None, id="lone-surrogate-in-a-namespace"),
    pytest.param({"version": 1, "languages": [{"tag": "de", "name": LONE_SURROGATE}]}, {}, None,
                 id="lone-surrogate-in-a-language-name"),
    pytest.param({"version": 1, "catalog": {"pl": "pl.json"}}, {"pl.json": nested_too_deep()}, None,
                 id="json-nested-too-deep"),
    pytest.param(nested_too_deep(), {}, None, id="declaration-nested-too-deep"),
    pytest.param({"version": 1, "catalog": {"pl": "loop.json"}}, {}, looping_link, id="path-is-a-link-loop"),
]


@pytest.mark.parametrize(("declaration", "files", "prepare"), FILES_THAT_BREAK_A_READER)
def test_an_extension_file_that_breaks_a_reader_is_refused_and_never_stops_the_load(
        tmp_path, declaration, files, prepare):
    module = extension(tmp_path, "broken", declaration, **files)
    if prepare:
        prepare(module.path)

    catalogs = load_with(module)

    assert i18n.module_errors(module.path, [module.path])
    assert_the_built_in_texts_are_served(catalogs)


SOURCELESS = {"greeting": "Cześć", "nav./demo": "Demo {who}"}
LINKED_FILES = ["i18n.json", "en.json", "pl.json", "pack.json", "system.json", "acl.json"]
OUTSIDE_TEXT = "Outside text 5b1e"
LINKED_CONTENT = {
    "i18n.json": {"version": 1, "languages": [{"tag": "xx", "name": OUTSIDE_TEXT}]},
    "en.json": {"greeting": OUTSIDE_TEXT}, "pl.json": {"farewell": OUTSIDE_TEXT},
    "pack.json": {"linked": {"greeting": OUTSIDE_TEXT}},
    "system.json": {"mode": {"type": "string", "label": OUTSIDE_TEXT}},
    "acl.json": {"operations": [{"id": "linked.run", "title": OUTSIDE_TEXT}]},
}


STAY_INSIDE = "must be relative, stay inside the module directory and end in .json"
LINK_REFUSALS = {
    "i18n.json": ["i18n.json: the file must be a regular file inside the module directory"],
    "en.json": [f"i18n.json: catalog.en: path 'en.json' {STAY_INSIDE}"],
    "pl.json": [f"i18n.json: catalog.pl: path 'pl.json' {STAY_INSIDE}"],
    "pack.json": [f"i18n.json: packs.pl: path 'pack.json' {STAY_INSIDE}"],
    "system.json": [], "acl.json": [],
}


def linked_module(root: Path):
    return extension(root, "linked", {"version": 1, "catalog": {"en": "en.json", "pl": "pl.json"},
                                      "packs": {"pl": "pack.json"}},
                     **{"en.json": {"greeting": "Hello"}, "pl.json": {"greeting": "Cześć"},
                        "pack.json": {"linked": {"greeting": "Hej"}}, "system_json": {"mode": {"label": "Mode"}},
                        "acl_json": {"operations": [{"id": "linked.run", "title": "Run"}]}})


def everything_read_from(module) -> str:
    catalogs = load_with(module)
    validated = i18n.module_errors(module.path, [module.path])
    source = i18n.english_source(i18n.Source("linked", module.path, module.path))
    return json.dumps([catalogs.languages, catalogs.messages, catalogs.fallback, validated, source,
                       i18n.manifest_messages(module.path)], ensure_ascii=False)


@pytest.mark.parametrize("linked", LINKED_FILES)
def test_a_link_that_leaves_the_module_directory_gives_no_text(tmp_path, linked):
    module = linked_module(tmp_path / "code")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(LINKED_CONTENT[linked]))
    (module.path / linked).unlink()
    (module.path / linked).symlink_to(outside)

    assert OUTSIDE_TEXT not in everything_read_from(module)
    assert_the_built_in_texts_are_served(load_with(module))
    assert i18n.module_errors(module.path, [module.path]) == LINK_REFUSALS[linked]


@pytest.mark.parametrize("linked", LINKED_FILES)
def test_a_link_that_stays_inside_the_module_directory_is_read(tmp_path, linked):
    module = linked_module(tmp_path / "code")
    (module.path / "real").mkdir()
    (module.path / "real" / linked).write_text(json.dumps(LINKED_CONTENT[linked]))
    (module.path / linked).unlink()
    (module.path / linked).symlink_to(Path("real") / linked)

    assert OUTSIDE_TEXT in everything_read_from(module)


def test_a_module_directory_that_is_itself_a_link_is_read_as_its_target(tmp_path):
    module = linked_module(tmp_path / "packages")
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "linked").symlink_to(module.path, target_is_directory=True)
    through_link = parse_module_dir(tmp_path / "code" / "linked")

    assert everything_read_from(through_link) == everything_read_from(module)
    assert load_with(through_link).messages["pl"]["linked"]["greeting"] == "Hej"


TRANSLATION_LAYERS = [
    pytest.param({"catalog": {"en": "en.json", "pl": "pl.json"}}, SOURCELESS, "catalog", id="catalog"),
    pytest.param({"catalog": {"en": "en.json"}, "packs": {"pl": "pl.json"}}, {"sourceless": SOURCELESS},
                 "packs", id="pack"),
    pytest.param({"catalog": {"en": "en.json"}, "overrides": {"pl": "pl.json"}}, {"sourceless": SOURCELESS},
                 "overrides", id="override"),
]


@pytest.mark.parametrize("layers, polish, layer", TRANSLATION_LAYERS)
def test_a_translation_with_no_english_source_names_no_param_in_any_layer(tmp_path, layers, polish, layer):
    module = extension(tmp_path, "sourceless", {"version": 1, **layers},
                       **{"en.json": {"greeting": "Hello"}, "pl.json": polish})

    assert i18n.module_errors(module.path, [module.path]) == [
        f"i18n.json: {layer}.pl: sourceless:nav./demo: unknown param 'who'"]
    assert load_with(module).messages["pl"]["sourceless"] == {"greeting": "Cześć"}


def test_a_translation_with_no_english_source_and_no_param_is_served(tmp_path):
    module = extension(tmp_path, "sourceless", {"version": 1, "catalog": {"pl": "pl.json"}},
                       **{"pl.json": {"nav./demo": "Demo", "joblink": "Otwórz"}})

    assert i18n.module_errors(module.path, [module.path]) == []
    assert set(load_with(module).messages["pl"]["sourceless"]) == {"nav./demo", "joblink"}


READ_SITES = ["i18n.json", "pl.json", "en.json", "pack.json", "system.json", "acl.json"]


@pytest.mark.parametrize("piped", READ_SITES)
def test_a_json_path_that_is_not_a_regular_file_is_skipped_before_it_is_opened(tmp_path, monkeypatch, piped):
    files = {"pl.json": {"greeting": "Cześć"}, "en.json": {"greeting": "Hello"},
             "pack.json": {"piped": {"greeting": "Hej"}}, "system_json": {"mode": {"label": "Mode"}},
             "acl_json": {"screens": []}}
    module = extension(tmp_path, "piped", {"version": 1, "catalog": {"en": "en.json", "pl": "pl.json"},
                                           "packs": {"pl": "pack.json"}}, **files)
    (module.path / piped).unlink()
    os.mkfifo(module.path / piped)
    opened = []
    read_text = Path.read_text

    def never_a_pipe(self, *args, **kwargs):
        if stat.S_ISFIFO(self.stat().st_mode):
            opened.append(self.name)
            return "{}"
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", never_a_pipe)

    catalogs = load_with(module)
    i18n.module_errors(module.path, [module.path])
    i18n.manifest_messages(module.path)

    assert opened == []
    assert_the_built_in_texts_are_served(catalogs)


@pytest.mark.parametrize("version", [True, 1.0, "1", None, 2, [1]])
def test_the_declaration_version_is_the_integer_one_and_no_value_that_only_equals_it(version):
    assert i18n.declaration_errors({"version": version}) == ["i18n.json: 'version' must be 1"]
    assert i18n.declaration_errors({"version": 1}) == []


def test_a_manifest_text_that_cannot_be_encoded_never_stops_the_load(tmp_path):
    module = parse_module_dir(write_module(tmp_path, "broken", system_json={
        "area/mode": {"type": "string", "label": LONE_SURROGATE, "description": "How it runs."},
        f"area/{LONE_SURROGATE}": {"type": "string", "label": "Label"}}))

    catalogs = load_with(module)

    assert catalogs.messages["en"]["broken"] == {"module.title": "Broken",
                                                 "config.area/mode.description": "How it runs."}
    assert_the_built_in_texts_are_served(catalogs)


def assert_the_built_in_texts_are_served(catalogs: i18n.Catalogs) -> None:
    assert set(catalogs.languages) >= set(REGISTRY)
    for tag in catalogs.languages:
        assert set(catalogs.login[tag].messages) == set(i18n.LOGIN_KEYS)
        served = {"messages": catalogs.messages[tag], "fallback": catalogs.fallback[tag],
                  "languages": catalogs.languages}
        json.dumps(served, ensure_ascii=False).encode("utf-8")
    assert catalogs.messages["pl"]["panel"]["login.title"] == load_with().messages["pl"]["panel"]["login.title"]


def test_any_failure_inside_one_source_is_contained_and_logged_by_its_class(tmp_path, monkeypatch, caplog):
    healthy = module_with_catalog(tmp_path, "healthy", en={"greeting": "Hello"}, pl={"greeting": "Cześć"})
    broken = module_with_catalog(tmp_path, "broken", en={"greeting": "Hello"}, pl={"greeting": "Cześć"})
    read = i18n._layer_entries

    def failing(source, *args):
        if source.name == "broken":
            raise LookupError(MARKER)
        return read(source, *args)

    monkeypatch.setattr(i18n, "_layer_entries", failing)
    with caplog.at_level(logging.WARNING, logger="agento.framework.i18n"):
        catalogs = load_with(healthy, broken)

    assert catalogs.messages["pl"]["healthy"] == {"greeting": "Cześć"}
    assert "broken" not in catalogs.messages["pl"]
    assert_the_built_in_texts_are_served(catalogs)
    assert [record.getMessage() for record in caplog.records] == ["i18n skip: module=broken reason=LookupError"]


def namespaces_validation_refuses(module, known: dict[str, Path]) -> set[str]:
    refused = i18n.module_errors(module.path, [*known.values(), module.path])
    return {namespace for namespace in ("ghost", "resting", "panel", "demo")
            if any(f"namespace '{namespace}' is not" in problem for problem in refused)}


def test_a_pack_namespace_that_is_not_an_enabled_source_is_not_served_and_the_rest_of_the_pack_is(tmp_path, caplog):
    demo = module_with_catalog(tmp_path, "demo", en={"greeting": "Hello"})
    resting = module_with_catalog(tmp_path, "resting", en={"greeting": "Hello"})
    tweak = pack(tmp_path, {"ghost": {"greeting": "duch"}, "resting": {"greeting": "sen"},
                            "demo": {"greeting": "Cześć"}, "panel": {"login.title": "Wejdź"}})

    with caplog.at_level(logging.WARNING, logger="agento.framework.i18n"):
        polish = load_with(demo, tweak).messages["pl"]

    assert namespaces_validation_refuses(tweak, {"demo": demo.path}) == {"ghost", "resting"}
    assert "ghost" not in polish and "resting" not in polish
    assert (polish["demo"], polish["panel"]["login.title"]) == ({"greeting": "Cześć"}, "Wejdź")
    assert sorted(record.getMessage() for record in caplog.records) == [
        f"i18n skip: module=tweak layer=packs language=pl namespace={namespace} reason=not an enabled namespace"
        for namespace in ("ghost", "resting")]
    assert load_with(demo, resting, tweak).messages["pl"]["resting"] == {"greeting": "sen"}


def login_versions(catalogs: i18n.Catalogs) -> dict[str, str]:
    return {tag: login.version for tag, login in catalogs.login.items()}


def test_the_login_version_follows_the_login_texts_only(tmp_path):
    plain = login_versions(load_with())
    private = load_with(module_with_catalog(tmp_path, "demo", en={"greeting": "Hello"}, pl={"greeting": "Cześć"}))
    reworded = login_versions(load_with(pack(tmp_path, {"panel": {"login.title": "Wejdź"}})))

    assert private.versions != load_with().versions
    assert login_versions(private) == plain
    assert reworded["pl"] != plain["pl"]
    assert reworded["en"] == plain["en"]
    assert all(len(version) == 16 for version in plain.values())


def test_the_sign_in_map_and_its_version_are_one_value_made_of_the_served_texts():
    catalogs = load_with(*fixture_manifests("pack_de", "override_first"))

    assert set(catalogs.login) == set(catalogs.languages)
    for tag, login in catalogs.login.items():
        panel, english = catalogs.messages[tag].get("panel", {}), catalogs.fallback[tag].get("panel", {})
        texts = [(key, panel[key] if key in panel else english[key]) for key in i18n.LOGIN_KEYS]
        canonical = json.dumps({"messages": {"panel": dict(texts)}, "fallback": {}}, sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False)
        assert list(login.messages.items()) == texts
        assert login.version == hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    german, english = catalogs.login["de"].messages, catalogs.login["en"].messages
    assert (german["login.submit"], german["login.username"]) == ("Anmelden", english["login.username"])


@pytest.fixture
def snapshot():
    i18n.install(load_with(LANGUAGE_OWNER))
    yield
    i18n.install(None)


@pytest.fixture
def no_snapshot(monkeypatch):
    i18n.install(None)
    monkeypatch.setattr(i18n, "languages", lambda: REGISTRY)


def conn_with_language(value: str | None) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None if value is None else {"value": value}
    return conn


@pytest.mark.parametrize(("env", "stored", "expected"), [
    (None, None, "en"),
    (None, "pl", "pl"),
    ("pl", "en", "pl"),
    (None, "de", "en"),
    ("zz", "pl", "en"),
])
def test_instance_language_is_env_then_db_then_the_default_and_only_a_registered_tag(
        monkeypatch, snapshot, env, stored, expected):
    monkeypatch.delenv(ENV, raising=False)
    if env is not None:
        monkeypatch.setenv(ENV, env)

    assert i18n.instance_language(conn_with_language(stored)) == expected


def test_the_snapshot_holds_the_file_default_of_the_instance_language(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    owner = parse_module_dir(write_module(tmp_path, "admin", config_json={"locale/language": "pl"}))

    assert load_with().default_language is None
    assert load_with(LANGUAGE_OWNER).default_language == "en"
    i18n.install(load_with(owner))
    try:
        assert i18n.instance_language(conn_with_language(None)) == "pl"
        assert i18n.instance_language(conn_with_language("en")) == "en"
    finally:
        i18n.install(None)


def test_instance_language_is_english_when_the_snapshot_has_no_owner_module(monkeypatch):
    monkeypatch.setenv(ENV, "pl")
    i18n.install(load_with())
    try:
        assert i18n.instance_language(conn_with_language("pl")) == "en"
    finally:
        i18n.install(None)


def test_instance_language_reads_no_module_file_when_a_snapshot_is_installed(monkeypatch, snapshot):
    monkeypatch.delenv(ENV, raising=False)

    def no_disk(*_a, **_kw):
        raise AssertionError("a module file was read after install")

    monkeypatch.setattr("agento.framework.module_status.read_module_status", no_disk)
    monkeypatch.setattr("agento.framework.config_resolver.read_config_defaults", no_disk)
    monkeypatch.setattr("agento.framework.core_config._find_module_dir", no_disk)

    assert i18n.instance_language(conn_with_language(None)) == "en"
    assert i18n.instance_language(conn_with_language("pl")) == "pl"


@pytest.mark.parametrize(("status", "stored", "expected"), [
    ({}, None, "en"),
    ({}, "pl", "pl"),
    ({"admin": False}, "pl", "en"),
])
def test_a_process_with_no_snapshot_reads_the_owner_module_off_disk(monkeypatch, no_snapshot, status, stored,
                                                                  expected):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr("agento.framework.module_status.read_module_status", lambda path=None: status)

    assert i18n.instance_language(conn_with_language(stored)) == expected


@pytest.mark.parametrize("strict", [True, False])
def test_a_failed_config_read_raises_only_for_a_strict_caller(monkeypatch, snapshot, strict):
    monkeypatch.delenv(ENV, raising=False)
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("db down")

    if strict:
        with pytest.raises(RuntimeError):
            i18n.instance_language(conn, strict=True)
        with pytest.raises(RuntimeError):
            i18n.account_language(conn)
    else:
        assert i18n.instance_language(conn) == "en"


def test_languages_answers_from_the_snapshot_and_opens_no_file(monkeypatch, snapshot):
    def no_disk(*_a, **_kw):
        raise AssertionError("a file was read after install")

    monkeypatch.setattr(i18n, "enabled_sources", no_disk)
    monkeypatch.setattr(i18n, "load", no_disk)

    assert i18n.languages() == REGISTRY
    assert i18n.current().languages == REGISTRY


NOT_A_FILE = {"directory": lambda path: path.mkdir(), "pipe": os.mkfifo,
              "dangling-link": lambda path: path.symlink_to("gone.json")}
REGULAR = "must be a regular file inside the module directory"
UNUSABLE_REFUSALS = {
    "i18n.json": f"i18n.json: the file {REGULAR}",
    "en.json": f"i18n.json: catalog.en: file 'en.json' {REGULAR}",
    "pl.json": f"i18n.json: catalog.pl: file 'pl.json' {REGULAR}",
    "pack.json": f"i18n.json: packs.pl: file 'pack.json' {REGULAR}",
}
UNUSABLE_SKIPS = {
    "i18n.json": "i18n skip: module=linked file=i18n.json reason=unusable file",
    "en.json": "i18n skip: module=linked layer=catalog language=en reason=unreadable file",
    "pl.json": "i18n skip: module=linked layer=catalog language=pl reason=unreadable file",
    "pack.json": "i18n skip: module=linked layer=packs language=pl reason=unreadable file",
    "system.json": "i18n skip: module=linked file=system.json reason=unusable file",
    "acl.json": "i18n skip: module=linked file=acl.json reason=unusable file",
}


@pytest.mark.parametrize("kind", NOT_A_FILE)
@pytest.mark.parametrize("name", READ_SITES)
def test_a_present_path_that_is_no_usable_file_is_logged_by_the_loader_and_refused_when_i18n_owns_it(
        tmp_path, monkeypatch, caplog, name, kind):
    module = linked_module(tmp_path / "code")
    (module.path / name).unlink()
    NOT_A_FILE[kind](module.path / name)
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **kw: pytest.fail(f"{self.name} was opened")
                        if self.name == name and self.parent == module.path else Path.read_bytes(self).decode())

    refused = i18n.module_errors(module.path, [module.path])
    with caplog.at_level(logging.WARNING):
        catalogs = load_with(module)

    assert refused == ([UNUSABLE_REFUSALS[name]] if name in UNUSABLE_REFUSALS else [])
    assert [record.getMessage() for record in caplog.records].count(UNUSABLE_SKIPS[name]) == 1
    assert_the_built_in_texts_are_served(catalogs)


@pytest.mark.parametrize("name", ["i18n.json", "system.json", "acl.json"])
def test_an_absent_optional_file_is_no_error_and_no_log_line(tmp_path, caplog, name):
    module = linked_module(tmp_path / "code")
    (module.path / name).unlink()

    with caplog.at_level(logging.WARNING):
        load_with(module)

    assert i18n.module_errors(module.path, [module.path]) == []
    assert caplog.records == []
