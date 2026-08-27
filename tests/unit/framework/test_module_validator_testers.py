"""module:validate must report a broken `tester` declaration, not run it."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agento.framework.module_validator import _validate_field_tester, validate_module

GOOD_MANIFEST = {"name": "m", "version": "0.1.0", "description": "test module"}


def _module(tmp_path: Path, system: dict, *, files: tuple[str, ...] = ()) -> Path:
    d = tmp_path / "m"
    (d / "src" / "testers").mkdir(parents=True, exist_ok=True)
    (d / "module.json").write_text(json.dumps(GOOD_MANIFEST))
    (d / "system.json").write_text(json.dumps(system))
    for rel in files:
        (d / rel).write_text("")
    return d


def _errors(tmp_path, system, **kw) -> list[str]:
    return validate_module(_module(tmp_path, system, **kw))


SMTP_OK = {
    "smtp_host": {"type": "string", "label": "H"},
    "smtp_pass": {
        "type": "obscure", "label": "P",
        "tester": {"kind": "smtp", "host": "{m/smtp_host}", "pass": "{m/smtp_pass}"},
    },
}


def test_a_valid_declarative_tester_produces_no_errors(tmp_path):
    assert _errors(tmp_path, SMTP_OK) == []


def test_a_valid_named_tester_produces_no_errors(tmp_path):
    """The name cannot be resolved from Python; the JS test is the gate."""
    assert _errors(tmp_path, {"secret": {"type": "obscure", "tester": "graph_credentials"}}) == []


def test_a_valid_local_tester_produces_no_errors(tmp_path):
    system = {
        "key": {
            "type": "obscure",
            "tester": {"kind": "local", "class": "src.testers.ssh.T"},
        },
    }
    assert _errors(tmp_path, system, files=("src/testers/ssh.py",)) == []


@pytest.mark.parametrize("raw", [42, [], {}, "", {"kind": ""}, {"kind": 7}, True])
def test_a_tester_that_is_not_a_declaration_is_an_error(tmp_path, raw):
    errors = _errors(tmp_path, {"f": {"type": "string", "tester": raw}})
    assert any("'f'" in e and "tester" in e for e in errors), errors


def test_an_unknown_kind_is_an_error_and_lists_the_known_ones(tmp_path):
    errors = _errors(tmp_path, {"f": {"type": "string", "tester": {"kind": "telnet"}}})
    assert any("telnet" in e and "smtp" in e and "http" in e and "local" in e for e in errors), errors


def test_a_local_tester_without_a_class_is_an_error(tmp_path):
    errors = _errors(tmp_path, {"f": {"type": "obscure", "tester": {"kind": "local"}}})
    assert any("class" in e for e in errors), errors


@pytest.mark.parametrize("class_path", [
    "..src.t.T",           # climbs out of the module directory
    ".src.t.T",            # empty leading segment
    "src..t.T",            # empty middle segment
    "src/t.T",             # a path separator, not a dotted path
    "T",                   # no module part at all
    "src.t-2.T",           # not an identifier
])
def test_a_class_path_that_escapes_the_module_is_an_error(tmp_path, class_path):
    """The plan promises the class lives inside the declaring module. `import_class`
    and `_resolve_class_path` both build the file path with a plain `.`->`/`
    replace, so that promise holds only if it is checked here."""
    errors = _validate_field_tester(
        tmp_path, "m", "pass", {"kind": "local", "class": class_path}, {"pass"},
    )
    assert any("inside" in e for e in errors), errors


def test_a_local_class_that_does_not_resolve_is_an_error(tmp_path):
    system = {"f": {"type": "obscure", "tester": {"kind": "local", "class": "src.testers.nope.T"}}}
    errors = _errors(tmp_path, system)
    assert any("does not resolve" in e for e in errors), errors


def test_a_builtin_kind_missing_its_required_key_is_an_error(tmp_path):
    errors = _errors(tmp_path, {"f": {"type": "obscure", "tester": {"kind": "smtp"}}})
    assert any("'host'" in e for e in errors), errors
    errors = _errors(tmp_path, {"f": {"type": "obscure", "tester": {"kind": "http"}}})
    assert any("'url'" in e for e in errors), errors


def test_a_placeholder_naming_another_module_is_an_error(tmp_path):
    """The run-time refusal is FOREIGN_PATH; this is the same rule, earlier."""
    system = {"f": {"type": "obscure", "tester": {"kind": "http", "url": "{jira/jira_host}/x"}}}
    errors = _errors(tmp_path, system)
    assert any("jira/jira_host" in e and "own module" in e for e in errors), errors


def test_a_placeholder_naming_an_absent_field_is_an_error(tmp_path):
    """A placeholder that resolves to None for ever is a silently dead test."""
    system = {"f": {"type": "obscure", "tester": {"kind": "smtp", "host": "{m/nope}"}}}
    errors = _errors(tmp_path, system)
    assert any("nope" in e and "not a field" in e for e in errors), errors


def test_a_placeholder_is_found_inside_a_nested_value(tmp_path):
    """`basic: ["{m/user}", "{m/token}"]` is the Jira shape — a check that only
    walked top-level strings would pass a broken declaration."""
    system = {
        "user": {"type": "string"},
        "f": {
            "type": "obscure",
            "tester": {"kind": "http", "url": "https://x/y", "basic": ["{m/user}", "{m/nope}"]},
        },
    }
    errors = _errors(tmp_path, system)
    assert any("nope" in e for e in errors), errors


def test_a_field_with_no_tester_is_not_reported(tmp_path):
    assert _errors(tmp_path, {"f": {"type": "string"}}) == []


def test_a_tool_field_tester_is_reported(tmp_path):
    """Tool fields are gated by `is_enabled`, and `tester_for_field` returns None
    for them — so a `tester` there would silently never run."""
    system = {"tools": {"t": {"is_enabled": {"type": "boolean", "tester": "x"}}}}
    errors = _errors(tmp_path, system)
    assert any("tool field" in e.lower() for e in errors), errors


def test_the_builtin_kind_table_matches_the_toolbox_probe_directory():
    """`BUILTIN_TOOLBOX_KINDS` names files in `src/agento/toolbox/probes/`. The
    table is duplicated across two languages by necessity — Python validates,
    JS runs — so the drift is guarded instead of trusted."""
    from agento.framework.config_test.protocols import (
        BUILTIN_TOOLBOX_KINDS,
        REQUIRED_SPEC_FIELDS,
    )

    probes = Path("src/agento/toolbox/probes")
    on_disk = sorted(p.stem for p in probes.glob("*.js"))
    assert on_disk == sorted(BUILTIN_TOOLBOX_KINDS)
    assert sorted(REQUIRED_SPEC_FIELDS) == sorted(BUILTIN_TOOLBOX_KINDS)

    # And each kind's required key must be the one the probe itself declares.
    for kind in BUILTIN_TOOLBOX_KINDS:
        source = (probes / f"{kind}.js").read_text()
        declared = re.search(r"export const required = \[([^\]]*)\]", source)
        assert declared, kind
        names = tuple(
            part.strip().strip("'\"")
            for part in declared.group(1).split(",")
            if part.strip()
        )
        assert names == REQUIRED_SPEC_FIELDS[kind], kind


def test_the_explicit_named_probe_form_is_accepted(tmp_path):
    """`"tester": "x"` is sugar for `{"kind": "toolbox", "name": "x"}` and the
    toolbox runs both. The validator used to reject the explicit form as an
    unknown kind, so `module:validate` failed a declaration that works."""
    system = {"secret": {"type": "obscure", "label": "S",
                         "tester": {"kind": "toolbox", "name": "graph_credentials"}}}
    assert _errors(tmp_path, system) == []


def test_the_explicit_named_probe_form_needs_a_name(tmp_path):
    system = {"secret": {"type": "obscure", "label": "S", "tester": {"kind": "toolbox"}}}
    errors = _errors(tmp_path, system)
    assert any("needs a non-empty 'name'" in e for e in errors), errors
