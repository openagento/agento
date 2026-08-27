"""Reading tester declarations off system.json — no bootstrap, never raises."""
from __future__ import annotations

import json
from pathlib import Path

from agento.framework.config_test.manifest import (
    KIND_LOCAL,
    KIND_TOOLBOX,
    enumerate_test_groups,
    enumerate_testable_fields,
    field_schema_for_path,
    field_schemas,
)
from agento.framework.config_test.manifest import (
    tester_for_field as _tester_for_field,
)


def _module(project_root: Path, name: str, system) -> Path:
    """A module under ``<project_root>/app/code/<name>`` — the layout
    ``iter_enabled_module_dirs(project_root)`` enumerates. ``system`` is written
    verbatim, so a test can pass a non-object to exercise the fail-closed path.
    """
    d = project_root / "app" / "code" / name
    d.mkdir(parents=True)
    (d / "module.json").write_text(json.dumps({"name": name, "version": "0.1.0"}))
    (d / "system.json").write_text(json.dumps(system))
    return d


def _from(paths, *modules):
    """Only the fixture's own modules. ``iter_enabled_module_dirs`` always yields the
    CORE modules too (they ship with the framework, whatever ``project_root`` is), and
    core modules carry real tester declarations — `agent_view/identity/ssh_private_key`
    among them. An equality assertion over the whole enumeration would therefore break
    every time a core module gains a test button, which is a feature, not a regression.
    """
    names = tuple(f"{m}/" for m in modules)
    return sorted(p for p in paths if p.startswith(names))


def _groups_from(groups, *modules):
    """`_from`, for the `(representative, paths)` shape."""
    names = tuple(f"{m}/" for m in modules)
    return [(rep, paths) for rep, paths in groups if rep.startswith(names)]


def test_a_bare_string_is_a_named_toolbox_tester(tmp_path):
    _module(tmp_path, "outlook", {"secret": {"type": "obscure", "tester": "graph_credentials"}})
    ref = _tester_for_field("outlook/secret", tmp_path)
    assert ref.kind == KIND_TOOLBOX
    assert ref.label == "graph_credentials"
    assert ref.class_path == ""


def test_an_inline_kind_is_a_toolbox_tester_too(tmp_path):
    """`smtp` and `http` run in the toolbox; the framework only needs to know it
    is not the local arm, so it never grows a branch per kind."""
    _module(tmp_path, "m", {"pass": {"type": "obscure", "tester": {"kind": "smtp", "host": "{m/h}"}}})
    ref = _tester_for_field("m/pass", tmp_path)
    assert ref.kind == KIND_TOOLBOX
    assert ref.label == "smtp"


def test_a_local_kind_carries_its_class(tmp_path):
    _module(tmp_path, "agent_view", {
        "identity/ssh_private_key": {
            "type": "obscure",
            "tester": {"kind": "local", "class": "src.testers.ssh_identity.SshIdentityTester"},
        },
    })
    ref = _tester_for_field("agent_view/identity/ssh_private_key", tmp_path)
    assert ref.kind == KIND_LOCAL
    assert ref.class_path == "src.testers.ssh_identity.SshIdentityTester"
    assert ref.module_dir.name == "agent_view"


def test_no_tester_is_none(tmp_path):
    _module(tmp_path, "m", {"host": {"type": "string"}})
    assert _tester_for_field("m/host", tmp_path) is None


def test_a_malformed_declaration_is_none_not_a_crash(tmp_path):
    """A hand-edited system.json must not break `config:get` or the admin TUI.
    `module:validate` is where a bad declaration is reported (Task 3)."""
    _module(tmp_path, "m", {
        "a": {"type": "string", "tester": 42},
        "b": {"type": "string", "tester": []},
        "c": {"type": "string", "tester": {}},              # no kind
        "d": {"type": "string", "tester": {"kind": 7}},
        "e": {"type": "string", "tester": ""},
    })
    for field in "abcde":
        assert _tester_for_field(f"m/{field}", tmp_path) is None, field


def test_an_unknown_module_or_path_is_none(tmp_path):
    assert _tester_for_field("nope/field", tmp_path) is None
    assert _tester_for_field("nope", tmp_path) is None
    assert _tester_for_field("", tmp_path) is None


def test_a_local_declaration_without_a_class_is_none(tmp_path):
    """The local arm is the only one that needs a class; without it there is
    nothing to run, and `module:validate` reports it."""
    _module(tmp_path, "m", {"f": {"type": "obscure", "tester": {"kind": "local"}}})
    assert _tester_for_field("m/f", tmp_path) is None


def test_the_schema_lookup_honours_project_root(tmp_path):
    """Two roots, two answers, no monkeypatching: a path-based lookup that
    searched the process-global module dirs would classify the wrong copy."""
    a, b = tmp_path / "a", tmp_path / "b"
    _module(a, "m", {"f": {"type": "obscure"}})
    _module(b, "m", {"f": {"type": "text"}})

    assert field_schema_for_path("m/f", a)["type"] == "obscure"
    assert field_schema_for_path("m/f", b)["type"] == "text"


def test_a_non_object_system_json_reads_as_unknown_not_as_a_crash(tmp_path):
    """`field_schema_for_path` is documented never to raise, but a system.json
    holding `[]` reached `.get` on a list. It must fail closed instead: `None`
    from `field_schema_for_path` is what makes a caller about to decrypt refuse."""
    _module(tmp_path, "m", [])
    assert field_schemas(tmp_path / "app" / "code" / "m") is None
    assert field_schema_for_path("m/f", tmp_path) is None


def test_an_unparsable_system_json_reads_as_unknown(tmp_path):
    d = tmp_path / "app" / "code" / "m"
    d.mkdir(parents=True)
    (d / "module.json").write_text(json.dumps({"name": "m", "version": "0.1.0"}))
    (d / "system.json").write_text("{ not json")
    assert field_schemas(d) is None
    assert _tester_for_field("m/f", tmp_path) is None


def test_enumerate_testable_fields_lists_every_declaring_field(tmp_path):
    _module(tmp_path, "m", {
        "pass": {"type": "obscure", "tester": {"kind": "smtp", "host": "{m/h}"}},
        "h": {"type": "string"},
        "other": {"type": "obscure", "tester": "named"},
    })
    _module(tmp_path, "n", {"x": {"type": "string"}})
    assert _from(enumerate_testable_fields(tmp_path), "m", "n") == ["m/other", "m/pass"]


def test_enumerate_test_groups_collapses_one_declaration_and_splits_two(tmp_path):
    """The key is the declaration, not the kind: two `http` testers over different
    credentials must stay two tests, or testing the user token would silently
    report on the admin one."""
    shared = {"kind": "smtp", "host": "{m/host}"}
    _module(tmp_path, "m", {
        "pass_a": {"type": "obscure", "tester": shared},
        "pass_b": {"type": "obscure", "tester": dict(shared)},
        "tok_user": {"type": "obscure", "tester": {"kind": "http", "url": "{m/u1}"}},
        "tok_admin": {"type": "obscure", "tester": {"kind": "http", "url": "{m/u2}"}},
        "plain": {"type": "string"},
    })
    groups = [g for g in enumerate_test_groups(tmp_path) if g[0].startswith("m/")]
    assert [g[0] for g in groups] == ["m/pass_a", "m/tok_user", "m/tok_admin"]
    assert groups[0][1] == ("m/pass_a", "m/pass_b")


def test_enumerate_skips_a_module_with_no_system_json(tmp_path):
    d = tmp_path / "app" / "code" / "m"
    d.mkdir(parents=True)
    (d / "module.json").write_text(json.dumps({"name": "m", "version": "0.1.0"}))
    assert _from(enumerate_testable_fields(tmp_path), "m") == []


def test_a_tool_field_never_has_a_tester(tmp_path):
    """`tools/<name>/<field>` paths are gated by `is_enabled`, not tested."""
    _module(tmp_path, "m", {"f": {"type": "string", "tester": "x"}})
    assert _tester_for_field("m/tools/f/is_enabled", tmp_path) is None


def test_the_two_spellings_of_a_named_probe_are_one_group(tmp_path):
    """`"x"` and `{"kind": "toolbox", "name": "x"}` reach the same probe over the
    same credential. Grouping on the raw JSON made them two logins, and the
    per-credential cooldown then answered COOLDOWN to the second."""
    _module(tmp_path, "m", {
        "a": {"type": "obscure", "tester": "graph_credentials"},
        "b": {"type": "obscure", "tester": {"kind": "toolbox", "name": "graph_credentials"}},
        "c": {"type": "obscure", "tester": {"kind": "toolbox", "name": "other_probe"}},
    })
    groups = _groups_from(enumerate_test_groups(tmp_path), "m")
    assert [paths for _, paths in groups] == [("m/a", "m/b"), ("m/c",)]


def test_two_builtin_declarations_over_different_credentials_stay_apart(tmp_path):
    """The spec is part of a built-in kind's identity — `jira_token` and
    `jira_admin_token` are one kind and two credentials."""
    _module(tmp_path, "m", {
        "t1": {"type": "obscure", "tester": {"kind": "http", "url": "u", "bearer": "{m/t1}"}},
        "t2": {"type": "obscure", "tester": {"kind": "http", "url": "u", "bearer": "{m/t2}"}},
    })
    assert len(_groups_from(enumerate_test_groups(tmp_path), "m")) == 2
