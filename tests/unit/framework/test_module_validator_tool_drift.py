"""server.tool() names must be declared, names must be unique, defaults must be owned."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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


# A literal `server.tool('<name>', …)` registration the scanner must report as drift.
_DETECTED = [
    pytest.param("export function register(server) { server.tool('demo_ping', 'x', {}, async () => {}); }",
                 "demo_ping", id="undeclared_literal"),
    pytest.param('server.tool("demo_pong", "x", {}, async () => {});', "demo_pong", id="double_quoted_name"),
    pytest.param("server.tool(\n  'demo_multi',\n  'desc',\n  {},\n);", "demo_multi", id="multiline"),
    # `this.server.tool(` / `ctx.server.tool(` ARE registrations.
    pytest.param("this.server.tool('demo_ping', 'x', {}, h);", "demo_ping", id="member_receiver"),
    pytest.param("server.tool ('demo_ping', 'x', {}, h);", "demo_ping", id="whitespace_before_paren"),
    pytest.param("server.tool\n  ('demo_ping', 'x', {}, h);", "demo_ping", id="newline_before_paren"),
    # The literal may be followed by ')' rather than ','.
    pytest.param("server.tool('demo_ping');", "demo_ping", id="single_argument"),
    # Only the line with a bare `/` is abandoned — the scan resumes at the next one.
    pytest.param("const ratio = a / b / c;\nserver.tool('demo_real', 'x', {}, h);", "demo_real",
                 id="slash_never_hides_a_later_line"),
    pytest.param("#!/usr/bin/env node\nserver.tool('demo_real', 'x', {}, h);", "demo_real",
                 id="hashbang_does_not_hide_a_later_line"),
    pytest.param("const r = (a + b) / c;\nconst z = a[0] / d;\nserver.tool('demo_real', 'x', {}, h);",
                 "demo_real", id="division_after_paren_or_bracket"),
]

# Text that looks like a registration but is not one — reporting it would invent a tool and
# abort somebody's `setup:upgrade`.
_IGNORED = [
    pytest.param("for (const [name, def] of x) { server.tool(name, def.description, def.schema, h); }\n"
                 "server.tool(tool.name, tool.description || '', shape, h);", id="dynamic_registration"),
    pytest.param("server.tool(`demo_${suffix}`, 'x', {}, h);", id="template_literal_name"),
    pytest.param("// e.g. server.tool('demo_docs', 'x', {}, h);\nconst a = 1;", id="line_comment"),
    pytest.param("/*\n * server.tool('demo_docs', 'x', {}, h);\n */\nconst a = 1;", id="block_comment"),
    pytest.param("""const example = 'server.tool("demo_docs", "x", {}, h)';""", id="single_quoted_string"),
    pytest.param("""const example = "server.tool('demo_docs', 'x', {}, h)";""", id="double_quoted_string"),
    pytest.param("const example = `server.tool('demo_docs', 'x', {}, h)`;", id="template_string"),
    pytest.param("""const s = 'it\\'s server.tool("demo_docs", 1)';""", id="escaped_quote_inside_string"),
    # `server.tool('demo_' + suffix, …)` must not be reported as tool 'demo_'.
    pytest.param("server.tool('demo_' + suffix, 'x', {}, h);", id="concatenated_name"),
    pytest.param("server.tool('demo_ping'.toUpperCase(), 'x', {}, h);", id="method_call_on_name"),
    # `mockserver.tool(` shares the substring but is not `server.tool(`.
    pytest.param("mockserver.tool('demo_ping', 'x', {}, h);", id="different_receiver"),
    pytest.param("server.tools('demo_ping', 'x');\nserver.toolFoo('demo_pong', 'x');", id="similar_method_name"),
    # Regex cases use UNESCAPED parentheses: with `\\(` the text is not a `server.tool(`
    # match at all, so the case would pass even with no regex handling — asserting nothing.
    pytest.param("const re = /server.tool('demo_docs')/;", id="regex_literal"),
    pytest.param("function f() { return /server.tool('demo_docs')/; }", id="regex_after_return"),
    pytest.param("function f() { throw /server.tool('demo_docs')/; }", id="regex_after_throw"),
    pytest.param("x.match(/server.tool('demo_docs')/);", id="regex_as_argument"),
    pytest.param("if (x) /server.tool('demo_docs')/.test(y);", id="regex_after_if"),
    pytest.param("const ok = a || /server.tool('demo_docs')/.test(b);", id="regex_after_logical_operator"),
    # `break` then a regex on the next line: any attempt to classify the `/` invented a tool.
    pytest.param("function f(x, y) {\n  while (x) {\n    break\n"
                 "    /server.tool('regex_only')/.test(y);\n  }\n}", id="asi_newline_before_regex"),
    # `default` is not a keyword any punctuation/keyword heuristic reliably covers.
    pytest.param("export default /server.tool('demo_docs')/;", id="export_default_regex"),
    pytest.param("class Demo extends /server.tool('demo_docs')/ {}", id="class_extends_regex"),
    # `#!node …` is a valid ES-module hashbang comment, not code.
    pytest.param("#!node server.tool('hashbang_only')\nconst a = 1;", id="hashbang_line"),
    # `this.#server.tool(...)` is a private field, not the injected MCP server.
    pytest.param("this.#server.tool('private_receiver_only', 'x', {}, h);", id="private_field_receiver"),
]


class TestLiteralDrift:
    @pytest.mark.parametrize(("js", "tool"), _DETECTED)
    def test_literal_registration_is_detected(self, tmp_path, js, tool):
        d = _mk(tmp_path, js=js)
        assert any(tool in e for e in _drift(validate_module(d)))

    @pytest.mark.parametrize("js", _IGNORED)
    def test_non_registration_is_ignored(self, tmp_path, js):
        d = _mk(tmp_path, js=js)
        assert validate_module(d) == []

    def test_declared_literal_tool_is_clean(self, tmp_path):
        d = _mk(tmp_path, tools=[_t("demo_ping")],
                js="export function register(server) { server.tool('demo_ping', 'x', {}, async () => {}); }")
        assert validate_module(d) == []

    @pytest.mark.parametrize("decoy", [
        """const s = "server.tool('demo_docs', 1)";""",
        "// server.tool('demo_docs', 1)",
        "const re = /server.tool('demo_docs')/;",
    ], ids=["string", "comment", "regex"])
    def test_real_registration_after_a_decoy_is_still_found(self, tmp_path, decoy):
        d = _mk(tmp_path, js=f"{decoy}\nserver.tool('demo_real', 'x', {{}}, h);")
        errors = _drift(validate_module(d))
        assert any("demo_real" in e for e in errors)
        assert not any("demo_docs" in e for e in errors)

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

    def test_a_registration_inside_a_control_paren_is_still_found(self, tmp_path):
        """The registration is on the same line as a regex, so the line is abandoned AFTER the
        call is matched — the real tool is reported and the regex contents are not."""
        d = _mk(tmp_path, tools=[_t("real_tool")],
                js="if (server.tool('real_tool')) /server.tool('regex_only')/.test(y);")
        assert validate_module(d) == []

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
