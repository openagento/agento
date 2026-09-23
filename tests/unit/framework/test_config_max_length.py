"""The generic `maxLength` field constraint — enforced at EVERY entry point.

A constraint implemented in one entry point and not the other is not a constraint, so the
rule lives in one shared helper (`framework/config_validation.py`) and this file asserts
each caller actually reaches it: `config:set`, the admin editor, and `module:validate` for
a malformed declaration.
"""
from __future__ import annotations

import json

import pytest

from agento.framework.config_validation import (
    MAX_LENGTH_TYPES,
    max_length_of,
    validate_field_value,
)


@pytest.fixture
def modules_dir(tmp_path, monkeypatch):
    core_dir = tmp_path / "core_modules"
    user_dir = tmp_path / "user_modules"
    core_dir.mkdir()
    user_dir.mkdir()
    av_dir = core_dir / "agent_view"
    av_dir.mkdir()
    (av_dir / "system.json").write_text(json.dumps({
        "identity/ssh_private_key": {
            "type": "obscure", "label": "SSH private key", "maxLength": 16,
        },
        "identity/ssh_public_key": {"type": "textarea", "label": "SSH public key"},
        # A neutral obscure field: `maxLength` is a GENERIC rule, so it must be asserted
        # on a field that carries no type-specific validation of its own. The private-key
        # field also parses its value (framework/cli/config.py `_is_private_key_field`),
        # which would decide these cases before the size rule was ever reached.
        "identity/api_token": {"type": "obscure", "label": "API token", "maxLength": 16},
    }))
    monkeypatch.setattr("agento.framework.bootstrap.CORE_MODULES_DIR", str(core_dir))
    monkeypatch.setattr("agento.framework.bootstrap.USER_MODULES_DIR", str(user_dir))
    return tmp_path


class TestSharedHelper:
    def test_no_limit_declared_is_always_valid(self):
        assert validate_field_value({"type": "obscure"}, "x" * 100_000) is None

    def test_at_the_limit_is_accepted(self):
        assert validate_field_value({"type": "obscure", "maxLength": 4}, "abcd") is None

    def test_one_byte_over_is_rejected_with_the_limit_named(self):
        error = validate_field_value({"type": "obscure", "maxLength": 4}, "abcde")
        assert error is not None
        assert "4" in error and "5" in error

    def test_the_limit_is_utf8_bytes_not_characters(self):
        """A `len(str)` implementation passes this value; a byte count does not."""
        assert validate_field_value({"type": "obscure", "maxLength": 4}, "ééé") is not None
        assert validate_field_value({"type": "obscure", "maxLength": 6}, "ééé") is None

    @pytest.mark.parametrize("field_type", sorted(MAX_LENGTH_TYPES))
    def test_every_text_type_can_carry_a_limit(self, field_type):
        assert max_length_of({"type": field_type, "maxLength": 8}) == 8

    @pytest.mark.parametrize("bad", ["16k", 0, -1, None, True, 1.5])
    def test_a_malformed_limit_is_ignored_rather_than_raising(self, bad):
        """The shared validator must never raise on a manifest typo — `module:validate`
        is where a bad declaration is reported."""
        assert max_length_of({"type": "obscure", "maxLength": bad}) is None
        assert validate_field_value({"type": "obscure", "maxLength": bad}, "x" * 99) is None


class TestConfigSetEntryPoint:
    def test_rejects_a_value_over_the_limit(self, modules_dir, capsys):
        from agento.framework.cli.config import _validate_config_value

        assert _validate_config_value(
            "agent_view/identity/api_token", "x" * 17,
        ) is False
        assert "16" in capsys.readouterr().out

    def test_accepts_a_value_at_the_limit(self, modules_dir):
        from agento.framework.cli.config import _validate_config_value

        assert _validate_config_value(
            "agent_view/identity/api_token", "x" * 16,
        ) is True

    def test_the_size_rule_runs_before_the_private_key_parse(self, modules_dir, capsys):
        """Order matters for the message: an oversized key is reported as oversized.

        Both rules reject this value. If the parse ran first the operator would be told
        the key is unparsable, and would go looking for a bad paste instead of a value
        that is simply too big for the declared ceiling.
        """
        from agento.framework.cli.config import _validate_config_value

        assert _validate_config_value(
            "agent_view/identity/ssh_private_key", "x" * 17,
        ) is False
        out = capsys.readouterr().out
        assert "16" in out and "does not parse" not in out

    def test_a_field_without_a_limit_is_unaffected(self, modules_dir):
        from agento.framework.cli.config import _validate_config_value

        assert _validate_config_value(
            "agent_view/identity/ssh_public_key", "x" * 100_000,
        ) is True


class TestAdminEditorEntryPoint:
    """`ResolvedField` carries the normalized limit so the editor can run the same rule."""

    def _field(self, **kw):
        from agento.framework.admin.data import ResolvedField

        defaults = dict(
            path="agent_view/identity/api_token",
            field_name="identity/api_token",
            value=None, display_value="", source="none",
            field_type="obscure", label="API token", obscure=True,
        )
        defaults.update(kw)
        return ResolvedField(**defaults)

    def _validate(self, field, value):
        from agento.framework.admin.screens.config import ConfigFieldEditorScreen

        return ConfigFieldEditorScreen._validate(
            type("S", (), {"_field": field})(), value,
        )

    def test_rejects_a_value_over_the_limit(self):
        error = self._validate(self._field(max_length=16), "x" * 17)
        assert error is not None and "16" in error

    def test_accepts_a_value_at_the_limit(self):
        assert self._validate(self._field(max_length=16), "x" * 16) is None

    def test_a_field_without_a_limit_is_unaffected(self):
        assert self._validate(self._field(), "x" * 100_000) is None

    def test_resolved_field_defaults_to_no_limit(self):
        assert self._field().max_length is None


class TestModuleValidateGate:
    """A schema typo must fail at setup:upgrade, not become a run-time TypeError."""

    def _errors(self, tmp_path, field_def):
        from agento.framework.module_validator import validate_module

        module_dir = tmp_path / "widget"
        module_dir.mkdir()
        (module_dir / "module.json").write_text(json.dumps({
            "name": "widget", "version": "1.0.0", "sequence": [],
        }))
        (module_dir / "system.json").write_text(json.dumps({"secret": field_def}))
        return validate_module(module_dir)

    @pytest.mark.parametrize("bad", ["16k", 0, -5, True])
    def test_a_non_positive_integer_is_rejected(self, tmp_path, bad):
        errors = self._errors(tmp_path, {"type": "obscure", "maxLength": bad})
        assert any("maxLength" in e and "secret" in e for e in errors), errors

    @pytest.mark.parametrize("field_type", ["select", "multiselect", "integer", "json"])
    def test_a_type_that_cannot_carry_a_limit_is_rejected(self, tmp_path, field_type):
        field_def = {"type": field_type, "maxLength": 16}
        if field_type in ("select", "multiselect"):
            field_def["options"] = [{"value": "a", "label": "A"}]
        errors = self._errors(tmp_path, field_def)
        assert any("maxLength" in e and field_type in e for e in errors), errors

    def test_a_well_formed_limit_passes(self, tmp_path):
        errors = self._errors(tmp_path, {"type": "obscure", "maxLength": 16})
        assert not any("maxLength" in e for e in errors), errors
