"""server.tool() names must be declared, names must be unique, defaults must be owned."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from agento.framework.module_validator import (
    validate_all,
    validate_module,
    validate_tool_namespace,
)

BASE_MANIFEST = {"name": "demo", "version": "1.0.0", "description": "demo module"}


def _mk(tmp_path, *, tools=None, js: str | None = None, js_name="demo.js", config=None, name="demo"):
    d = tmp_path / name
    (d / "toolbox").mkdir(parents=True)
    manifest = {**BASE_MANIFEST, "name": name}
    if tools is not None:
        manifest["tools"] = tools
    (d / "module.json").write_text(json.dumps(manifest))
    if js is not None:
        (d / "toolbox" / js_name).write_text(js)
    if config is not None:
        (d / "config.json").write_text(json.dumps(config))
    return d


def _t(name, **extra):
    return {"type": "mcp", "name": name, "description": "d", "toolset": "demo", **extra}


def _drift(errors):
    return [e for e in errors if "not declared in module.json tools[]" in e]


class TestLiteralDrift:
    def test_undeclared_literal_tool_is_an_error(self, tmp_path):
        d = _mk(tmp_path, js="export function register(server) { server.tool('demo_ping', 'x', {}, async () => {}); }")
        assert any("demo_ping" in e for e in _drift(validate_module(d)))

    def test_declared_literal_tool_is_clean(self, tmp_path):
        d = _mk(tmp_path, tools=[_t("demo_ping")],
                js="export function register(server) { server.tool('demo_ping', 'x', {}, async () => {}); }")
        assert validate_module(d) == []

    def test_double_quoted_name_is_detected(self, tmp_path):
        d = _mk(tmp_path, js='server.tool("demo_pong", "x", {}, async () => {});')
        assert any("demo_pong" in e for e in _drift(validate_module(d)))

    def test_multiline_registration_is_detected(self, tmp_path):
        d = _mk(tmp_path, js="server.tool(\n  'demo_multi',\n  'desc',\n  {},\n);")
        assert any("demo_multi" in e for e in _drift(validate_module(d)))

    def test_dynamic_registration_is_ignored(self, tmp_path):
        d = _mk(tmp_path, js="for (const [name, def] of x) { server.tool(name, def.description, def.schema, h); }\nserver.tool(tool.name, tool.description || '', shape, h);")
        assert validate_module(d) == []

    def test_template_literal_name_is_ignored(self, tmp_path):
        d = _mk(tmp_path, js="server.tool(`demo_${suffix}`, 'x', {}, h);")
        assert validate_module(d) == []

    def test_line_comment_example_is_ignored(self, tmp_path):
        d = _mk(tmp_path, js="// e.g. server.tool('demo_docs', 'x', {}, h);\nconst a = 1;")
        assert validate_module(d) == []

    def test_block_comment_example_is_ignored(self, tmp_path):
        d = _mk(tmp_path, js="/*\n * server.tool('demo_docs', 'x', {}, h);\n */\nconst a = 1;")
        assert validate_module(d) == []

    def test_single_quoted_string_containing_the_call_is_ignored(self, tmp_path):
        d = _mk(tmp_path, js="""const example = 'server.tool("demo_docs", "x", {}, h)';""")
        assert validate_module(d) == []

    def test_double_quoted_string_containing_the_call_is_ignored(self, tmp_path):
        d = _mk(tmp_path, js="""const example = "server.tool('demo_docs', 'x', {}, h)";""")
        assert validate_module(d) == []

    def test_template_string_containing_the_call_is_ignored(self, tmp_path):
        d = _mk(tmp_path, js="const example = `server.tool('demo_docs', 'x', {}, h)`;")
        assert validate_module(d) == []

    def test_escaped_quote_inside_a_string_does_not_end_it(self, tmp_path):
        d = _mk(tmp_path, js="""const s = 'it\\'s server.tool("demo_docs", 1)';""")
        assert validate_module(d) == []

    def test_real_registration_after_a_decoy_string_is_still_found(self, tmp_path):
        d = _mk(tmp_path, js="""const s = "server.tool('demo_docs', 1)";\nserver.tool('demo_real', 'x', {}, h);""")
        errors = _drift(validate_module(d))
        assert any("demo_real" in e for e in errors)
        assert not any("demo_docs" in e for e in errors)

    def test_real_registration_after_a_comment_decoy_is_still_found(self, tmp_path):
        d = _mk(tmp_path, js="// server.tool('demo_docs', 1)\nserver.tool('demo_real', 'x', {}, h);")
        errors = _drift(validate_module(d))
        assert any("demo_real" in e for e in errors)
        assert not any("demo_docs" in e for e in errors)

    def test_concatenated_name_is_dynamic_not_a_partial_literal(self, tmp_path):
        """`server.tool('demo_' + suffix, …)` must not be reported as tool 'demo_'."""
        d = _mk(tmp_path, js="server.tool('demo_' + suffix, 'x', {}, h);")
        assert validate_module(d) == []

    def test_method_call_on_the_name_is_dynamic(self, tmp_path):
        d = _mk(tmp_path, js="server.tool('demo_ping'.toUpperCase(), 'x', {}, h);")
        assert validate_module(d) == []

    def test_a_different_receiver_does_not_match(self, tmp_path):
        """`mockserver.tool(` shares the substring but is not `server.tool(`."""
        d = _mk(tmp_path, js="mockserver.tool('demo_ping', 'x', {}, h);")
        assert validate_module(d) == []

    def test_a_member_receiver_still_matches(self, tmp_path):
        """`this.server.tool(` / `ctx.server.tool(` ARE registrations."""
        d = _mk(tmp_path, js="this.server.tool('demo_ping', 'x', {}, h);")
        assert any("demo_ping" in e for e in _drift(validate_module(d)))

    def test_whitespace_before_the_paren_still_matches(self, tmp_path):
        d = _mk(tmp_path, js="server.tool ('demo_ping', 'x', {}, h);")
        assert any("demo_ping" in e for e in _drift(validate_module(d)))

    def test_newline_before_the_paren_still_matches(self, tmp_path):
        d = _mk(tmp_path, js="server.tool\n  ('demo_ping', 'x', {}, h);")
        assert any("demo_ping" in e for e in _drift(validate_module(d)))

    def test_single_argument_call_matches(self, tmp_path):
        """The literal may be followed by ')' rather than ','."""
        d = _mk(tmp_path, js="server.tool('demo_ping');")
        assert any("demo_ping" in e for e in _drift(validate_module(d)))

    def test_a_similar_method_name_does_not_match(self, tmp_path):
        d = _mk(tmp_path, js="server.tools('demo_ping', 'x');\nserver.toolFoo('demo_pong', 'x');")
        assert validate_module(d) == []

    # These must use UNESCAPED parentheses: with `\\(` the text is not a `server.tool(`
    # match at all, so the test would pass even with no regex handling — asserting nothing.

    def test_regex_literal_containing_the_call_is_ignored(self, tmp_path):
        d = _mk(tmp_path, js="const re = /server.tool('demo_docs')/;")
        assert validate_module(d) == []

    def test_regex_literal_after_a_return_is_ignored(self, tmp_path):
        d = _mk(tmp_path, js="function f() { return /server.tool('demo_docs')/; }")
        assert validate_module(d) == []

    def test_regex_literal_after_a_throw_is_ignored(self, tmp_path):
        d = _mk(tmp_path, js="function f() { throw /server.tool('demo_docs')/; }")
        assert validate_module(d) == []

    def test_regex_literal_as_an_argument_is_ignored(self, tmp_path):
        d = _mk(tmp_path, js="x.match(/server.tool('demo_docs')/);")
        assert validate_module(d) == []

    def test_regex_literal_in_statement_position_after_if_is_ignored(self, tmp_path):
        d = _mk(tmp_path, js="if (x) /server.tool('demo_docs')/.test(y);")
        assert validate_module(d) == []

    def test_regex_literal_after_a_logical_operator_is_ignored(self, tmp_path):
        d = _mk(tmp_path, js="const ok = a || /server.tool('demo_docs')/.test(b);")
        assert validate_module(d) == []

    def test_known_limitation_a_slash_on_the_line_hides_a_registration(self, tmp_path):
        """ACCEPTED GAP, pinned so it is a decision rather than a surprise.

        A bare `/` is division or the start of a regex, and deciding which is a full lexical-goal
        problem — ASI, brace grammar, postfix operators and the enclosing construct all feed it.
        Three token heuristics were tried here and each was defeated by valid JavaScript, in the
        direction that matters most: inventing a tool and aborting somebody's `setup:upgrade`. So
        the scanner abandons the rest of the line instead. It can MISS; it cannot raise a false
        error. Both shapes below are the same accepted gap.

        The exact check is `toolbox/tests/tool-declaration.test.js`, which executes `register()`;
        the runtime drift WARN backs it up for a deployment's own modules.
        """
        for i, js in enumerate((
            "const ratio = a / b; server.tool('ghost_tool', 'x', {}, h);",
            "const ratio = { value: 1 } / server.tool('ghost_tool') / 2;",
        )):
            d = _mk(tmp_path, js=js, name=f"demo{i}")
            assert validate_module(d) == [], js

    def test_a_slash_never_hides_a_registration_on_a_later_line(self, tmp_path):
        """Only the offending line is abandoned — the scan resumes at the next one."""
        d = _mk(tmp_path, js="const ratio = a / b / c;\nserver.tool('demo_real', 'x', {}, h);")
        assert any("demo_real" in e for e in _drift(validate_module(d)))

    def test_asi_newline_before_a_regex_is_not_a_false_positive(self, tmp_path):
        """`break` then a regex on the next line. Any attempt to classify the `/` got this wrong
        and invented a tool; abandoning the line cannot."""
        d = _mk(tmp_path, js=(
            "function f(x, y) {\n  while (x) {\n    break\n"
            "    /server.tool('regex_only')/.test(y);\n  }\n}"))
        assert validate_module(d) == []

    def test_a_registration_inside_a_control_paren_is_still_found(self, tmp_path):
        """The registration is on the same line as a regex, so the line is abandoned AFTER the
        call is matched — the real tool is reported and the regex contents are not."""
        d = _mk(tmp_path, tools=[_t("real_tool")],
                js="if (server.tool('real_tool')) /server.tool('regex_only')/.test(y);")
        assert validate_module(d) == []

    def test_a_hashbang_line_is_not_scanned(self, tmp_path):
        """`#!node …` is a valid ES-module hashbang comment, not code."""
        d = _mk(tmp_path, js="#!node server.tool('hashbang_only')\nconst a = 1;")
        assert validate_module(d) == []

    def test_a_private_field_receiver_is_not_the_framework_server(self, tmp_path):
        """`this.#server.tool(...)` is a private field, not the injected MCP server."""
        d = _mk(tmp_path, js="this.#server.tool('private_receiver_only', 'x', {}, h);")
        assert validate_module(d) == []

    def test_a_hashbang_does_not_hide_a_later_registration(self, tmp_path):
        d = _mk(tmp_path, js="#!/usr/bin/env node\nserver.tool('demo_real', 'x', {}, h);")
        assert any("demo_real" in e for e in _drift(validate_module(d)))

    def test_export_default_regex_is_not_a_false_positive(self, tmp_path):
        """`default` is not a keyword any punctuation/keyword heuristic reliably covers, and a
        false error here would abort setup:upgrade."""
        d = _mk(tmp_path, js="export default /server.tool('demo_docs')/;")
        assert validate_module(d) == []

    def test_class_extends_regex_is_not_a_false_positive(self, tmp_path):
        d = _mk(tmp_path, js="class Demo extends /server.tool('demo_docs')/ {}")
        assert validate_module(d) == []

    def test_division_after_a_paren_or_bracket_is_not_a_false_positive(self, tmp_path):
        d = _mk(tmp_path, js="const r = (a + b) / c;\nconst z = a[0] / d;\nserver.tool('demo_real', 'x', {}, h);")
        assert any("demo_real" in e for e in _drift(validate_module(d)))

    def test_real_registration_after_a_regex_decoy_is_still_found(self, tmp_path):
        d = _mk(tmp_path, js="const re = /server.tool('demo_docs')/;\nserver.tool('demo_real', 'x', {}, h);")
        errors = _drift(validate_module(d))
        assert any("demo_real" in e for e in errors)
        assert not any("demo_docs" in e for e in errors)

    def test_all_toolbox_files_are_scanned(self, tmp_path):
        d = _mk(tmp_path, js="server.tool('demo_a', 'x', {}, h);", js_name="a.js")
        (d / "toolbox" / "b.js").write_text("server.tool('demo_b', 'x', {}, h);")
        errors = _drift(validate_module(d))
        assert any("demo_a" in e for e in errors)
        assert any("demo_b" in e for e in errors)

    def test_module_without_toolbox_dir_is_clean(self, tmp_path):
        d = tmp_path / "demo"
        d.mkdir()
        (d / "module.json").write_text(json.dumps(BASE_MANIFEST))
        assert validate_module(d) == []

    def test_error_message_names_the_file_and_suggests_the_entry(self, tmp_path):
        d = _mk(tmp_path, js="server.tool('demo_ping', 'x', {}, h);", js_name="ping.js")
        (err,) = _drift(validate_module(d))
        assert "ping.js" in err
        assert '"toolset": "demo"' in err


class TestConfigDefaultOwnership:
    def test_default_for_a_declared_tool_is_clean(self, tmp_path):
        d = _mk(tmp_path, tools=[_t("demo_ping")], config={"tools/demo_ping/is_enabled": "1"})
        assert validate_module(d) == []

    def test_default_for_an_undeclared_tool_is_rejected(self, tmp_path):
        d = _mk(tmp_path, tools=[_t("demo_ping")], config={"tools/other_tool/is_enabled": "1"})
        assert any("this module does not declare" in e for e in validate_module(d))

    def test_unrelated_config_keys_are_ignored(self, tmp_path):
        d = _mk(tmp_path, tools=[_t("demo_ping")], config={"timezone": "UTC", "toolbox/url": "x"})
        assert validate_module(d) == []


class TestToolNamespace:
    """The cross-manifest check, exercised directly AND through both entry points."""

    def test_duplicate_tool_name_is_rejected(self):
        decls = [
            ("a", [{"type": "mcp", "name": "clash", "description": "d", "toolset": "a"}]),
            ("b", [{"type": "mcp", "name": "clash", "description": "d", "toolset": "b"}]),
        ]
        results = validate_tool_namespace(decls)
        assert any("globally unique" in e for errs in results.values() for e in errs)

    def test_distinct_names_are_clean(self):
        decls = [
            ("a", [{"type": "mcp", "name": "a_tool", "description": "d", "toolset": "a"}]),
            ("b", [{"type": "mcp", "name": "b_tool", "description": "d", "toolset": "b"}]),
        ]
        assert validate_tool_namespace(decls) == {}

    def test_distinct_names_in_one_module_are_clean(self):
        decls = [("a", [
            {"type": "mcp", "name": "a_one", "description": "d", "toolset": "a"},
            {"type": "mcp", "name": "a_two", "description": "d", "toolset": "a"},
        ])]
        assert validate_tool_namespace(decls) == {}

    def test_cross_module_pass_ignores_same_module_duplicates(self):
        """_validate_module owns those (see TestSameModuleDuplicates) — reporting them here
        too would double up every message."""
        decls = [("a", [
            {"type": "mcp", "name": "dup", "description": "d", "toolset": "a"},
            {"type": "mcp", "name": "dup", "description": "d", "toolset": "a"},
        ])]
        assert validate_tool_namespace(decls) == {}

    def test_validate_all_runs_the_namespace_check(self, tmp_path):
        core = tmp_path / "core"
        user = tmp_path / "user"
        core.mkdir()
        user.mkdir()
        for parent, mod in ((core, "a"), (user, "b")):
            d = parent / mod
            d.mkdir()
            (d / "module.json").write_text(json.dumps({
                **BASE_MANIFEST, "name": mod,
                "tools": [{"type": "mcp", "name": "clash", "description": "d", "toolset": mod}],
            }))
        results = validate_all(core, user)
        assert any("globally unique" in e for errs in results.values() for e in errs)

    def test_setup_upgrade_aborts_on_a_duplicate_enabled_tool_name(self, tmp_path):
        """The whole point: setup validates module-by-module, so without the shared
        helper a duplicate name would sail past and reuse a name-keyed grant."""
        import logging

        import pytest

        from agento.framework.setup import ModuleValidationError, _validate_manifests

        enabled = []
        for mod in ("a", "b"):
            d = tmp_path / mod
            d.mkdir()
            tools = [{"type": "mcp", "name": "clash", "description": "d", "toolset": mod}]
            (d / "module.json").write_text(json.dumps({**BASE_MANIFEST, "name": mod, "tools": tools}))
            m = MagicMock()
            m.name = mod
            m.path = d
            m.tools = tools
            enabled.append(m)

        with pytest.raises(ModuleValidationError):
            _validate_manifests(enabled, logging.getLogger("test"))

    def test_setup_upgrade_passes_on_the_real_shipped_modules(self):
        import logging

        from agento.framework.bootstrap import CORE_MODULES_DIR
        from agento.framework.module_loader import scan_modules
        from agento.framework.module_status import filter_enabled
        from agento.framework.setup import _validate_manifests

        # The real shipped set must pass — same assertion setup:upgrade makes.
        _validate_manifests(filter_enabled(scan_modules(CORE_MODULES_DIR)), logging.getLogger("test"))


class TestSameModuleDuplicates:
    """Locally detectable, so a scoped `module:validate <name>` must catch them."""

    def test_duplicate_name_in_one_manifest_is_rejected(self, tmp_path):
        d = _mk(tmp_path, tools=[_t("dup"), _t("dup")])
        assert any("declared twice in this module" in e for e in validate_module(d))

    def test_distinct_names_in_one_manifest_are_clean(self, tmp_path):
        d = _mk(tmp_path, tools=[_t("one"), _t("two")])
        assert validate_module(d) == []

    def test_a_duplicate_is_reported_exactly_once_end_to_end(self, tmp_path):
        """validate_all runs both passes; the message must not appear twice."""
        core = tmp_path / "core"
        user = tmp_path / "user"
        core.mkdir()
        user.mkdir()
        d = core / "demo"
        d.mkdir()
        (d / "module.json").write_text(json.dumps({
            **BASE_MANIFEST, "tools": [
                {"type": "mcp", "name": "dup", "description": "d", "toolset": "demo"},
                {"type": "mcp", "name": "dup", "description": "d", "toolset": "demo"},
            ],
        }))
        errs = validate_all(core, user)["demo"]
        assert sum("declared twice in this module" in e for e in errs) == 1


def test_shipped_core_modules_are_clean():
    """The real repo must stay clean — this is the regression guard."""
    from agento.framework.bootstrap import CORE_MODULES_DIR

    modules = Path(CORE_MODULES_DIR)
    offenders = {}
    for entry in sorted(modules.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        if errs := validate_module(entry):
            offenders[entry.name] = errs
    assert offenders == {}
