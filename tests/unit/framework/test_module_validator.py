"""Tests for module validation."""
from __future__ import annotations

import json
from pathlib import Path

from agento.framework.module_validator import validate_all, validate_module

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_DIR = ROOT / "app" / "code" / "_example"


class TestValidateModule:
    def test_valid_module_passes(self, tmp_path: Path):
        mod = tmp_path / "good"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "good",
            "version": "1.0.0",
            "description": "A good module",
            "tools": [],
            "log_servers": [],
        }))
        (mod / "config.json").write_text("{}")

        errors = validate_module(mod)
        assert errors == []

    def test_missing_module_json(self, tmp_path: Path):
        mod = tmp_path / "empty"
        mod.mkdir()

        errors = validate_module(mod)
        assert any("module.json not found" in e for e in errors)

    def test_missing_required_fields(self, tmp_path: Path):
        mod = tmp_path / "incomplete"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({"name": "incomplete"}))

        errors = validate_module(mod)
        assert any("version" in e for e in errors)
        assert any("description" in e for e in errors)

    def test_tool_missing_toolset_flagged(self, tmp_path: Path):
        mod = tmp_path / "tool-no-toolset"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "tool-no-toolset",
            "version": "1.0.0",
            "description": "Tool without a toolset",
            "tools": [{"type": "mysql", "name": "mysql_x", "description": "X"}],
        }))

        errors = validate_module(mod)
        assert any("tools[0] missing 'toolset'" in e for e in errors)

    def test_mysql_root_tool_name_must_end_in_root(self, tmp_path: Path):
        """Full-access capability must be visible in the tool NAME: enablement is keyed by
        name, so promoting a tool in place would silently inherit its read-only grant."""
        mod = tmp_path / "unmarked-root"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "unmarked-root",
            "version": "1.0.0",
            "description": "Full-access tool without the name marker",
            "tools": [{"type": "mysql_root", "name": "mysql_sandbox", "description": "X", "toolset": "G"}],
        }))

        errors = validate_module(mod)
        assert any("mysql_sandbox" in e and "_root" in e for e in errors), errors

    def test_reserved_root_suffix_rejected_on_a_read_only_type(self, tmp_path: Path):
        """The suffix is RESERVED, not merely required: a read-only tool may not squat a
        '_root' name, because flipping only its type would then escalate it in place while
        keeping the name-keyed is_enabled grant."""
        mod = tmp_path / "squatter"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "squatter",
            "version": "1.0.0",
            "description": "Read-only tool squatting a reserved name",
            "tools": [{"type": "mysql", "name": "customer_db_root", "description": "X", "toolset": "G"}],
        }))

        errors = validate_module(mod)
        assert any("customer_db_root" in e and "reserved" in e for e in errors), errors

    def test_reserved_root_suffix_rejected_on_any_non_full_access_type(self, tmp_path: Path):
        mod = tmp_path / "squatter2"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "squatter2",
            "version": "1.0.0",
            "description": "Non-SQL tool squatting a reserved name",
            "tools": [{"type": "opensearch", "name": "os_products_root", "description": "X", "toolset": "G"}],
        }))

        errors = validate_module(mod)
        assert any("os_products_root" in e and "reserved" in e for e in errors), errors

    def test_mysql_root_tool_with_marker_passes(self, tmp_path: Path):
        mod = tmp_path / "marked-root"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "marked-root",
            "version": "1.0.0",
            "description": "Full-access tool with the name marker",
            "tools": [{"type": "mysql_root", "name": "mysql_sandbox_root", "description": "X", "toolset": "G"}],
            "log_servers": [],
        }))
        (mod / "config.json").write_text("{}")

        assert validate_module(mod) == []

    def test_read_only_mysql_tool_needs_no_marker(self, tmp_path: Path):
        mod = tmp_path / "plain-mysql"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "plain-mysql",
            "version": "1.0.0",
            "description": "Read-only tool",
            "tools": [{"type": "mysql", "name": "mysql_reporting", "description": "X", "toolset": "G"}],
            "log_servers": [],
        }))
        (mod / "config.json").write_text("{}")

        assert validate_module(mod) == []

    def test_tool_with_toolset_passes(self, tmp_path: Path):
        mod = tmp_path / "tool-ok"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "tool-ok",
            "version": "1.0.0",
            "description": "Tool with a toolset",
            "tools": [{"type": "mysql", "name": "mysql_x", "description": "X", "toolset": "Group"}],
            "log_servers": [],
        }))
        (mod / "config.json").write_text("{}")

        errors = validate_module(mod)
        assert errors == []

    def test_invalid_di_json_class(self, tmp_path: Path):
        mod = tmp_path / "bad-di"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "bad-di",
            "version": "1.0.0",
            "description": "Bad DI",
            "tools": [],
        }))
        (mod / "di.json").write_text(json.dumps({
            "commands": [{"class": "src.commands.nonexistent.FakeCommand"}],
        }))

        errors = validate_module(mod)
        assert any("does not resolve" in e for e in errors)

    def test_example_module_valid(self):
        """The actual _example module should pass validation."""
        errors = validate_module(EXAMPLE_DIR)
        assert errors == [], f"Example module errors: {errors}"

    def test_sequence_not_a_list(self, tmp_path: Path):
        mod = tmp_path / "bad-seq"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "bad-seq",
            "version": "1.0.0",
            "description": "Bad sequence",
            "sequence": "not-a-list",
        }))
        errors = validate_module(mod)
        assert any("'sequence' must be an array" in e for e in errors)

    def test_sequence_entry_not_string(self, tmp_path: Path):
        mod = tmp_path / "bad-entry"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "bad-entry",
            "version": "1.0.0",
            "description": "Bad entry",
            "sequence": [123],
        }))
        errors = validate_module(mod)
        assert any("must be strings" in e for e in errors)

    def test_valid_sequence(self, tmp_path: Path):
        mod = tmp_path / "good-seq"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "good-seq",
            "version": "1.0.0",
            "description": "Good sequence",
            "sequence": ["core"],
        }))
        errors = validate_module(mod)
        assert errors == []

    def test_invalid_json_file(self, tmp_path: Path):
        mod = tmp_path / "bad-json"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "bad-json",
            "version": "1.0.0",
            "description": "Bad JSON",
            "tools": [],
        }))
        (mod / "config.json").write_text("{invalid json")

        errors = validate_module(mod)
        assert any("config.json" in e for e in errors)


class TestSelectFieldValidation:
    """Validation of select/multiselect fields with options."""

    def _make_module(self, tmp_path: Path, system: dict) -> Path:
        mod = tmp_path / "sel"
        mod.mkdir(exist_ok=True)
        (mod / "module.json").write_text(json.dumps({
            "name": "sel", "version": "1.0.0", "description": "Select test",
        }))
        (mod / "system.json").write_text(json.dumps(system))
        return mod

    def test_select_field_requires_options(self, tmp_path: Path):
        mod = self._make_module(tmp_path, {
            "strategy": {"type": "select", "label": "Strategy"},
        })
        errors = validate_module(mod)
        assert any("requires 'options'" in e for e in errors)

    def test_multiselect_field_requires_options(self, tmp_path: Path):
        mod = self._make_module(tmp_path, {
            "tags": {"type": "multiselect", "label": "Tags"},
        })
        errors = validate_module(mod)
        assert any("requires 'options'" in e for e in errors)

    def test_select_field_with_valid_options_passes(self, tmp_path: Path):
        mod = self._make_module(tmp_path, {
            "strategy": {
                "type": "select", "label": "Strategy",
                "options": [
                    {"value": "copy", "label": "Copy"},
                    {"value": "symlink", "label": "Symlink"},
                ],
            },
        })
        errors = validate_module(mod)
        assert errors == []

    def test_select_option_missing_value_or_label(self, tmp_path: Path):
        mod = self._make_module(tmp_path, {
            "strategy": {
                "type": "select", "label": "Strategy",
                "options": [{"value": "copy"}],
            },
        })
        errors = validate_module(mod)
        assert any("must have 'value' and 'label'" in e for e in errors)

    def test_select_option_not_an_object(self, tmp_path: Path):
        mod = self._make_module(tmp_path, {
            "strategy": {
                "type": "select", "label": "Strategy",
                "options": ["copy", "symlink"],
            },
        })
        errors = validate_module(mod)
        assert any("must be an object" in e for e in errors)

    def test_options_on_non_select_type_errors(self, tmp_path: Path):
        mod = self._make_module(tmp_path, {
            "name": {
                "type": "string", "label": "Name",
                "options": [{"value": "a", "label": "A"}],
            },
        })
        errors = validate_module(mod)
        assert any("only select/multiselect support options" in e for e in errors)

    def test_options_not_a_list_errors(self, tmp_path: Path):
        mod = self._make_module(tmp_path, {
            "strategy": {
                "type": "select", "label": "Strategy",
                "options": "invalid",
            },
        })
        errors = validate_module(mod)
        assert any("options must be an array" in e for e in errors)


class TestRegexIdentityTypesValidation:
    """Rule-2: di.json `regex_identity_types` must be a list of canonical identity-type strings
    (^[a-z][a-z0-9_]{0,31}$) so a malformed manifest is rejected BEFORE any DB change."""

    def _make_module(self, tmp_path: Path, regex_types) -> Path:
        mod = tmp_path / "regex-mod"
        mod.mkdir(exist_ok=True)
        (mod / "module.json").write_text(json.dumps({
            "name": "regex-mod", "version": "1.0.0", "description": "Regex types",
        }))
        (mod / "di.json").write_text(json.dumps({"regex_identity_types": regex_types}))
        return mod

    def test_valid_list_passes(self, tmp_path: Path):
        errors = validate_module(self._make_module(tmp_path, ["outlook_sender", "teams_channel"]))
        assert errors == []

    def test_absent_is_fine(self, tmp_path: Path):
        mod = tmp_path / "regex-mod"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "regex-mod", "version": "1.0.0", "description": "no di",
        }))
        (mod / "di.json").write_text(json.dumps({"commands": []}))
        assert validate_module(mod) == []

    def test_non_list_rejected(self, tmp_path: Path):
        errors = validate_module(self._make_module(tmp_path, "outlook_sender"))
        assert any("'regex_identity_types' must be an array" in e for e in errors)

    def test_explicit_null_rejected(self, tmp_path: Path):
        # An explicit `null` is a PRESENT-but-malformed declaration — must be rejected as "not an
        # array" (validation keys on key presence, not `is not None`), otherwise a setup-valid
        # module's advertised routing capability silently vanishes at bootstrap.
        errors = validate_module(self._make_module(tmp_path, None))
        assert any("'regex_identity_types' must be an array" in e for e in errors)

    def test_empty_string_entry_rejected(self, tmp_path: Path):
        errors = validate_module(self._make_module(tmp_path, [""]))
        assert any("regex_identity_types[0]" in e and "^[a-z]" in e for e in errors)

    def test_over_32_chars_rejected(self, tmp_path: Path):
        errors = validate_module(self._make_module(tmp_path, ["a" * 33]))
        assert any("regex_identity_types[0]" in e for e in errors)

    def test_exactly_32_chars_passes(self, tmp_path: Path):
        # ^[a-z][a-z0-9_]{0,31}$ allows up to 32 chars total (fits VARCHAR(32)).
        errors = validate_module(self._make_module(tmp_path, ["a" + "b" * 31]))
        assert errors == []

    def test_leading_trailing_whitespace_rejected(self, tmp_path: Path):
        errors = validate_module(self._make_module(tmp_path, [" outlook_sender "]))
        assert any("regex_identity_types[0]" in e for e in errors)

    def test_control_char_rejected(self, tmp_path: Path):
        errors = validate_module(self._make_module(tmp_path, ["outlook\tsender"]))
        assert any("regex_identity_types[0]" in e for e in errors)

    def test_uppercase_rejected(self, tmp_path: Path):
        errors = validate_module(self._make_module(tmp_path, ["Outlook_Sender"]))
        assert any("regex_identity_types[0]" in e for e in errors)

    def test_non_string_entry_rejected(self, tmp_path: Path):
        errors = validate_module(self._make_module(tmp_path, [123]))
        assert any("regex_identity_types[0]" in e for e in errors)

    def test_reports_the_offending_index(self, tmp_path: Path):
        # a good entry at [0], a bad one at [1] -> only [1] flagged
        errors = validate_module(self._make_module(tmp_path, ["good_type", "BadType"]))
        assert any("regex_identity_types[1]" in e for e in errors)
        assert not any("regex_identity_types[0]" in e for e in errors)


class TestValidateAllSequenceCross:
    """Cross-module sequence validation: deps must exist on disk."""

    def _make_module(self, parent: Path, name: str, sequence: list[str] | None = None):
        mod = parent / name
        mod.mkdir()
        manifest = {"name": name, "version": "1.0.0", "description": f"{name} module"}
        if sequence is not None:
            manifest["sequence"] = sequence
        (mod / "module.json").write_text(json.dumps(manifest))

    def test_missing_sequence_dep_flagged(self, tmp_path: Path):
        core = tmp_path / "core_mods"
        core.mkdir()
        self._make_module(core, "a", sequence=["nonexistent"])
        results = validate_all(core, tmp_path / "empty")
        assert "a" in results
        assert any("nonexistent" in e and "not found" in e for e in results["a"])

    def test_valid_sequence_dep_passes(self, tmp_path: Path):
        core = tmp_path / "core_mods"
        core.mkdir()
        self._make_module(core, "base")
        self._make_module(core, "child", sequence=["base"])
        results = validate_all(core, tmp_path / "empty")
        assert "child" not in results


class TestAllowEnvFalseIsCaughtBeforeDeploy:
    """`allowEnv: false` means the resolver IGNORES `CONFIG__*` for that field. A deploy that sets
    one anyway would run with a value it believes is applied, so `setup:upgrade` aborts on it —
    but only if the validator computes the SAME env key the resolver does."""

    def _module(self, tmp_path: Path, name: str, fields: dict) -> Path:
        mod = tmp_path / name
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": name, "version": "1.0.0", "description": "d", "tools": [], "log_servers": [],
        }))
        (mod / "config.json").write_text("{}")
        (mod / "system.json").write_text(json.dumps(fields))
        return mod

    def test_a_plain_module_and_field(self, tmp_path: Path, monkeypatch):
        mod = self._module(tmp_path, "vault", {"the_secret": {"type": "obscure", "allowEnv": False}})
        monkeypatch.setenv("CONFIG__VAULT__THE_SECRET", "x")
        errors = validate_module(mod)
        assert any("allowEnv:false" in e for e in errors), errors

    def test_a_HYPHENATED_module_name(self, tmp_path: Path, monkeypatch):
        """`my-app` maps to `CONFIG__MY_APP__*`. A validator that did not replace `-` looked for
        `CONFIG__MY-APP__*`, which no shell can even set, so the check passed vacuously."""
        mod = self._module(tmp_path, "my-app", {"the_secret": {"type": "obscure", "allowEnv": False}})
        monkeypatch.setenv("CONFIG__MY_APP__THE_SECRET", "x")
        errors = validate_module(mod)
        assert any("CONFIG__MY_APP__THE_SECRET" in e for e in errors), errors

    def test_a_SLASH_KEYED_field(self, tmp_path: Path, monkeypatch):
        """`identity/ssh_private_key` maps to `...__IDENTITY__SSH_PRIVATE_KEY`."""
        mod = self._module(
            tmp_path, "vault", {"identity/ssh_private_key": {"type": "obscure", "allowEnv": False}}
        )
        monkeypatch.setenv("CONFIG__VAULT__IDENTITY__SSH_PRIVATE_KEY", "x")
        errors = validate_module(mod)
        assert any("CONFIG__VAULT__IDENTITY__SSH_PRIVATE_KEY" in e for e in errors), errors

    def test_the_remediation_never_puts_the_secret_in_argv(self, tmp_path: Path, monkeypatch):
        mod = self._module(tmp_path, "vault", {"the_secret": {"type": "obscure", "allowEnv": False}})
        monkeypatch.setenv("CONFIG__VAULT__THE_SECRET", "x")
        message = next(e for e in validate_module(mod) if "allowEnv:false" in e)
        assert "<value>" not in message
        assert "stdin" in message

    def test_no_error_when_the_variable_is_not_set(self, tmp_path: Path, monkeypatch):
        mod = self._module(tmp_path, "vault", {"the_secret": {"type": "obscure", "allowEnv": False}})
        monkeypatch.delenv("CONFIG__VAULT__THE_SECRET", raising=False)
        assert not any("allowEnv:false" in e for e in validate_module(mod))



class TestSecurityMetadataTyposAreRejected:
    """`access` and `allowEnv` fail OPEN on a typo: the resolver compares `access` with the exact
    string `toolbox_only` and refuses ENV only for the exact boolean `false`. So a manifest that
    writes `"toolbox-only"` or `"allowEnv": "false"` deploys with the boundary silently off. The
    validator is the only place that can see the difference, so it must refuse them."""

    def _module(self, tmp_path: Path, fields: dict) -> Path:
        mod = tmp_path / "vault"
        mod.mkdir(exist_ok=True)
        (mod / "module.json").write_text(json.dumps({
            "name": "vault", "version": "1.0.0", "description": "d", "tools": [], "log_servers": [],
        }))
        (mod / "config.json").write_text("{}")
        (mod / "system.json").write_text(json.dumps(fields))
        return mod

    def test_a_misspelled_access_value_is_an_error(self, tmp_path: Path):
        mod = self._module(tmp_path, {"s": {"type": "obscure", "access": "toolbox-only"}})
        assert any("invalid access 'toolbox-only'" in e for e in validate_module(mod))

    def test_a_non_string_access_value_is_an_error_not_a_crash(self, tmp_path: Path):
        """`x not in {…}` raises TypeError on a list or a dict, which aborts the whole
        validation run — the deploy then sees a traceback instead of the finding."""
        for bad in ([], {}, 7, True):
            mod = self._module(tmp_path, {"s": {"type": "obscure", "access": bad}})
            assert any("invalid access" in e for e in validate_module(mod)), bad

    def test_the_correct_access_value_passes(self, tmp_path: Path):
        mod = self._module(tmp_path, {"s": {"type": "obscure", "access": "toolbox_only"}})
        assert not any("invalid access" in e for e in validate_module(mod))

    def test_no_manifest_value_type_can_abort_the_run(self, tmp_path: Path):
        """The class: every membership test on a manifest value must report, never raise.
        A run that dies on one field also skips every check after it."""
        for key in ("type", "options_source", "access"):
            for bad in ([], {}, 7):
                mod = self._module(tmp_path, {"s": {"type": "select", key: bad, "options": ["a"]}})
                validate_module(mod)  # must not raise

    def test_a_string_allowEnv_is_an_error(self, tmp_path: Path):
        """`"false"` is a truthy string, so `env_allowed` returns True and the field stays
        settable from the environment — the exact opposite of what the author wrote."""
        mod = self._module(tmp_path, {"s": {"type": "obscure", "allowEnv": "false"}})
        assert any("non-boolean allowEnv" in e for e in validate_module(mod))

    def test_a_boolean_allowEnv_passes(self, tmp_path: Path):
        mod = self._module(tmp_path, {"s": {"type": "obscure", "allowEnv": False}})
        assert not any("non-boolean allowEnv" in e for e in validate_module(mod))

    def test_a_string_scope_flag_is_an_error(self, tmp_path: Path):
        """Same shape one level out: `is_scope_allowed` reads the flag as truthy, so
        `"showInAgentView": "false"` leaves the field editable at agent_view scope."""
        mod = self._module(tmp_path, {"s": {"type": "string", "showInAgentView": "false"}})
        assert any("non-boolean showInAgentView" in e for e in validate_module(mod))

    def test_every_shipped_module_manifest_declares_these_keys_correctly(self):
        """The class guard over the real manifests, not a fixture: any shipped `system.json`
        that mistypes one of the three security keys fails here."""
        import agento
        from agento.framework.config_schema import env_allowed, is_toolbox_only

        root = Path(agento.__file__).parent / "modules"
        for system_path in root.glob("*/system.json"):
            fields = json.loads(system_path.read_text())
            for name, schema in fields.items():
                if not isinstance(schema, dict):
                    continue
                if "access" in schema:
                    assert is_toolbox_only(schema), f"{system_path.parent.name}/{name}: access"
                for key in ("allowEnv", "showInDefault", "showInWorkspace", "showInAgentView"):
                    if key in schema:
                        assert isinstance(schema[key], bool), f"{system_path.parent.name}/{name}: {key}"
                if schema.get("allowEnv") is not None:
                    assert env_allowed(schema) is (schema["allowEnv"] is not False)


class TestMalformedManifestValuesAreReportedNotCrashes:
    """The class, stated once: a manifest value of an unexpected JSON type must be REPORTED.
    Two failure shapes hide behind that. A membership or lookup test on an unhashable value
    (`{…}.get([])`, `[] not in {…}`) raises TypeError and aborts the whole run, so the deploy
    sees a traceback and none of the other findings; and a `if value and …` guard skips every
    FALSY wrong type (`[]`, `{}`, `0`, `""`, `false` all read as "the key is absent") and lets
    the manifest deploy unvalidated. Both end with a security-relevant key never checked."""

    def _module(self, parent: Path, name: str, manifest: dict, system: dict | None = None):
        mod = parent / name
        mod.mkdir(parents=True, exist_ok=True)
        (mod / "module.json").write_text(json.dumps(manifest))
        if system is not None:
            (mod / "system.json").write_text(json.dumps(system))
        return mod

    def _base(self, **over):
        m = {"name": "m", "version": "1.0.0", "description": "d"}
        m.update(over)
        return m

    def test_an_unhashable_tool_type_is_reported(self, tmp_path: Path):
        mod = self._module(tmp_path, "m", self._base(tools=[
            {"type": ["mysql_query"], "name": "m_read", "description": "d", "toolset": "t"},
        ]))
        assert any("'type' must be a string" in e for e in validate_module(mod))

    def test_an_unhashable_module_name_is_reported(self, tmp_path: Path):
        mod = self._module(tmp_path, "m", self._base(name=["m"]))
        assert any("'name' must be a non-empty string" in e for e in validate_module(mod))

    def test_a_blank_module_name_is_reported(self, tmp_path: Path):
        mod = self._module(tmp_path, "m", self._base(name="  "))
        assert any("'name' must be a non-empty string" in e for e in validate_module(mod))

    def test_every_falsy_wrong_field_type_is_reported(self, tmp_path: Path):
        """`if field_type and …` passed all of these — the field then deploys with no type
        check at all, next to the `access`/`allowEnv` keys the same block validates."""
        for bad in ([], {}, 0, "", False):
            mod = self._module(tmp_path, f"m{len(str(bad))}{bad!r}"[:12], self._base(),
                               system={"s": {"type": bad}})
            assert any("invalid type" in e for e in validate_module(mod)), bad

    def test_an_absent_field_type_stays_allowed(self, tmp_path: Path):
        """The bound of the fix: `type` is optional, so a missing key is not an error."""
        mod = self._module(tmp_path, "m", self._base(), system={"s": {"label": "S"}})
        assert not any("invalid type" in e for e in validate_module(mod))

    def test_validate_all_survives_every_malformed_shape_at_once(self, tmp_path: Path):
        """validate_all keys a dict on the manifest name and iterates `sequence` — both
        raise on a wrong type, and a raise there loses the findings for EVERY module."""
        core = tmp_path / "core"
        self._module(core, "a", self._base(name={"not": "a string"}))
        self._module(core, "b", self._base(name="b", sequence={"not": "a list"}))
        self._module(core, "c", self._base(name="c", sequence=[["nested"]]))
        results = validate_all(core, tmp_path / "empty")
        assert any("'name' must be a non-empty string" in e for e in results.get("a", []))
        assert any("must be an array" in e for e in results.get("b", []))
        assert any("must be strings" in e for e in results.get("c", []))

    def test_a_non_list_di_section_is_reported(self, tmp_path: Path):
        """A section that is a dict iterates its KEYS and finds nothing; a scalar raises
        TypeError. Either way an unresolvable class ships unreported."""
        for bad in ({"class": "src.x.Y"}, 7, "src.x.Y"):
            mod = self._module(tmp_path / f"d{len(str(bad))}", "m", self._base())
            (mod / "di.json").write_text(json.dumps({"commands": bad}))
            assert any("'commands' must be an array" in e for e in validate_module(mod)), bad

    def test_a_non_string_class_path_is_reported(self, tmp_path: Path):
        """`_resolve_class_path` calls `.rsplit` — AttributeError on anything but a string."""
        for bad in ([], {}, 7, None, False):
            mod = self._module(tmp_path / f"c{len(str(bad))}{bad!r}"[:24], "m", self._base())
            (mod / "di.json").write_text(json.dumps({"commands": [{"class": bad}]}))
            (mod / "events.json").write_text(json.dumps({"job_claim_after": [{"class": bad}]}))
            errors = validate_module(mod)
            assert any("di.json: commands 'class' must be a string" in e for e in errors), bad
            assert any("events.json: observer 'class' must be a string" in e for e in errors), bad

    def test_no_manifest_key_of_any_json_type_can_raise(self, tmp_path: Path):
        """The class guard. Every key the validator reads, set to every JSON type it is not:
        validation must return findings, never propagate an exception. A raise anywhere here
        loses every OTHER finding in the same run — including the security-relevant ones."""
        bad_values = ([], {}, 0, "", False, None, 7, [{}], {"k": []})
        manifest_keys = ("name", "version", "description", "tools", "log_servers",
                         "sequence", "toolsets", "skills")
        di_keys = ("channels", "workflows", "commands", "agent_harnesses",
                   "regex_identity_types", "observers")
        for i, bad in enumerate(bad_values):
            for key in manifest_keys:
                mod = self._module(tmp_path / f"m{i}", key, self._base(**{key: bad}),
                                   system={"s": bad})
                (mod / "config.json").write_text(json.dumps({"tools/x/is_enabled": bad}))
                validate_module(mod)  # must not raise
            for key in di_keys:
                mod = self._module(tmp_path / f"d{i}", key, self._base())
                (mod / "di.json").write_text(json.dumps({key: bad}))
                (mod / "events.json").write_text(json.dumps({"some_event_after": bad}))
                validate_module(mod)  # must not raise
        validate_all(tmp_path / f"m{len(bad_values) - 1}", tmp_path / "no-such-dir")
