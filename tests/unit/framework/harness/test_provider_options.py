"""Provider-specific config fields are shown only for the providers that need them.

``agent_view/provider_options/base_url`` is Ollama's endpoint override. On OpenRouter it is
meaningless, and an empty box an operator can never usefully fill is a standing invitation
to fill it wrongly. So a provider declares the options it needs in its own ``di.json`` and
the ``system.json`` field names the option it belongs to.

The rule this mechanism exists to respect: the condition lives with the PROVIDER. No core
module and no framework file names a provider — the same agent-agnosticism that keeps
provider ids out of framework branches.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.admin.data import ModuleSchema, get_resolved_fields
from agento.framework.harness import (
    ModelProviderDescriptor,
    is_provider_option_hidden,
    provider_option_names,
)
from agento.framework.scoped_config import Scope

BASE_URL_FIELD = {"type": "string", "label": "Provider base URL", "provider_option": "base_url"}
REPO = Path(__file__).resolve().parents[4]


class TestTheDeclarationIsParsed:
    def test_a_provider_declares_the_options_it_needs(self):
        desc = ModelProviderDescriptor.from_declaration({
            "id": "ollama", "credential_required": False, "provider_options": ["base_url"],
        })
        assert desc.provider_options == ("base_url",)

    def test_declaring_none_is_the_default(self):
        desc = ModelProviderDescriptor.from_declaration({
            "id": "openai", "credential_required": False,
        })
        assert desc.provider_options == ()

    @pytest.mark.parametrize("bad", ["base_url", {"a": 1}, 5, False, 0, "", None])
    def test_a_non_array_is_rejected_including_falsey_ones(self, bad):
        """`provider_options: false` / `0` / `""` must be an ERROR, not "declared nothing".

        The first version normalised with `or []`, so every falsey non-array was silently
        accepted — the test name said "non-array is rejected" while only truthy non-arrays
        were tried. A second version still let an explicit top-level `null` through by
        checking `is None` instead of key presence. Absence is the only thing that may mean
        none, and `provider_options: null` is not absence.
        """
        with pytest.raises(ValueError, match="provider_options must be an array"):
            ModelProviderDescriptor.from_declaration({
                "id": "x", "credential_required": False, "provider_options": bad,
            })

    def test_an_absent_key_is_the_only_way_to_declare_nothing(self):
        assert ModelProviderDescriptor.from_declaration({
            "id": "x", "credential_required": False,
        }).provider_options == ()
        assert ModelProviderDescriptor.from_declaration({
            "id": "x", "credential_required": False, "provider_options": [],
        }).provider_options == ()

    @pytest.mark.parametrize("bad", [5, None, True, ["nested"], {"a": 1}, "", "   "])
    def test_a_non_string_entry_is_rejected_not_coerced(self, bad):
        """`str(raw)` turned `[5]` into `("5",)` — a manifest typo becoming a valid name."""
        with pytest.raises(ValueError, match="non-empty strings"):
            ModelProviderDescriptor.from_declaration({
                "id": "x", "credential_required": False, "provider_options": [bad],
            })

    def test_a_malformed_name_is_rejected(self):
        # These become `agent_view/provider_options/<name>` paths, so they are bounded ids
        # like every other declared name — not free-form strings from a third-party file.
        with pytest.raises(ValueError):
            ModelProviderDescriptor.from_declaration({
                "id": "x", "credential_required": False, "provider_options": ["Base URL!"],
            })

    def test_a_duplicate_name_is_rejected(self):
        with pytest.raises(ValueError, match="duplicate provider_options"):
            ModelProviderDescriptor.from_declaration({
                "id": "x", "credential_required": False,
                "provider_options": ["base_url", "base_url"],
            })


class TestVisibilityAgainstTheShippedDeclarations:
    """Reads the real manifests off disk — the same path admin uses."""

    def test_ollama_declares_base_url(self):
        assert provider_option_names("pi", "ollama") == frozenset({"base_url"})

    def test_openrouter_declares_nothing(self):
        assert provider_option_names("pi", "openrouter") == frozenset()

    def test_visible_for_the_provider_that_needs_it(self):
        assert not is_provider_option_hidden(BASE_URL_FIELD, harness="pi", provider="ollama")

    def test_hidden_for_a_provider_that_does_not(self):
        assert is_provider_option_hidden(BASE_URL_FIELD, harness="pi", provider="openrouter")

    def test_hidden_for_another_harness_entirely(self):
        assert is_provider_option_hidden(
            BASE_URL_FIELD, harness="claude", provider="anthropic",
        )

    @pytest.mark.parametrize(
        "harness,provider",
        [(None, None), ("pi", None), (None, "ollama"), ("", "")],
    )
    def test_an_unset_selection_leaves_the_field_visible(self, harness, provider):
        """Hiding a field the operator still needs to set is the worse failure, and at the
        default scope there may legitimately be no provider chosen yet."""
        assert not is_provider_option_hidden(
            BASE_URL_FIELD, harness=harness, provider=provider,
        )

    def test_an_unresolvable_provider_leaves_the_field_visible(self):
        """A disabled module or a typo'd config value is MISSING INFORMATION, not a
        provider that declared no options — the two must not collapse into "hide"."""
        assert not is_provider_option_hidden(
            BASE_URL_FIELD, harness="pi", provider="not-a-real-provider",
        )

    def test_a_field_without_the_key_is_never_hidden(self):
        assert not is_provider_option_hidden(
            {"type": "string"}, harness="pi", provider="openrouter",
        )


class TestTheAgentViewManifestUsesTheMechanism:
    """Guards the wiring itself: either side alone silently does nothing."""

    def test_the_field_declares_the_option(self):
        system = json.loads(
            (REPO / "src/agento/modules/agent_view/system.json").read_text()
        )
        assert system["provider_options/base_url"]["provider_option"] == "base_url"

    def test_ollama_claims_it_in_di_json(self):
        di = json.loads((REPO / "src/agento/modules/pi/di.json").read_text())
        providers = {p["id"]: p for p in di["agent_harnesses"][0]["providers"]}
        assert providers["ollama"].get("provider_options") == ["base_url"]
        assert "provider_options" not in providers["openrouter"]


class TestAdminHidesTheField:
    """End of the chain: the TUI must actually drop the row."""

    def _fields(self, resolved: dict):
        schema = ModuleSchema(
            name="agent_view",
            fields={"provider_options/base_url": dict(BASE_URL_FIELD),
                    "model": {"type": "string", "label": "Model"}},
            tools={},
            module_path=None,
        )
        with patch(
            "agento.framework.admin.data.get_module_schemas", return_value=[schema]
        ), patch(
            "agento.framework.admin.data.read_config_defaults", return_value={}
        ), patch(
            "agento.framework.scoped_config.build_scoped_overrides", return_value={}
        ), patch(
            "agento.framework.scoped_config.load_scoped_db_overrides", return_value={}
        ), patch(
            "agento.framework.config_resolver.ScopedConfigService.resolve_all",
            return_value=resolved,
        ):
            conn = MagicMock()
            cursor = MagicMock()
            cursor.fetchone.return_value = {"workspace_id": 1}
            conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
            conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            return get_resolved_fields(conn, "agent_view", Scope.AGENT_VIEW, 42)

    def test_shown_on_ollama(self):
        paths = [f.field_name for f in self._fields(
            {"agent_view/harness": "pi", "agent_view/provider": "ollama"},
        )]
        assert "provider_options/base_url" in paths

    def test_hidden_on_openrouter(self):
        fields = self._fields(
            {"agent_view/harness": "pi", "agent_view/provider": "openrouter"},
        )
        paths = [f.field_name for f in fields]
        assert "provider_options/base_url" not in paths
        # ...and the rest of the module is unaffected.
        assert "model" in paths

    def test_shown_when_nothing_is_selected_yet(self):
        paths = [f.field_name for f in self._fields({})]
        assert "provider_options/base_url" in paths


class TestValidatorRejectsAnUnclaimedOption:
    """A `provider_option` no provider declares hides the field FOREVER with no error
    anywhere — the "guard that cannot fire" shape this codebase keeps paying for."""

    def test_an_unclaimed_option_is_an_error(self, tmp_path):
        from agento.framework.module_validator import validate_module

        module = tmp_path / "broken"
        module.mkdir()
        (module / "module.json").write_text(json.dumps({
            "name": "broken", "version": "1.0.0", "sequence": [],
        }))
        (module / "system.json").write_text(json.dumps({
            "some_field": {"type": "string", "provider_option": "no_such_option"},
        }))
        errors = validate_module(module)
        assert any("no_such_option" in e for e in errors)

    def test_a_claimed_option_passes(self, tmp_path):
        from agento.framework.module_validator import validate_module

        module = tmp_path / "fine"
        module.mkdir()
        (module / "module.json").write_text(json.dumps({
            "name": "fine", "version": "1.0.0", "sequence": [],
        }))
        (module / "system.json").write_text(json.dumps({
            "some_field": {"type": "string", "provider_option": "base_url"},
        }))
        assert not [e for e in validate_module(module) if "provider_option" in e]

    def test_a_non_string_option_is_an_error(self, tmp_path):
        from agento.framework.module_validator import validate_module

        module = tmp_path / "bad_type"
        module.mkdir()
        (module / "module.json").write_text(json.dumps({
            "name": "bad_type", "version": "1.0.0", "sequence": [],
        }))
        (module / "system.json").write_text(json.dumps({
            "some_field": {"type": "string", "provider_option": True},
        }))
        errors = validate_module(module)
        assert any("must be a non-empty string" in e for e in errors)


class TestDisablingTheDeclaringModuleKeepsOthersValid:
    """Every module must be safely disableable — a core project rule.

    `agent_view` is a core module and always enabled, but the only provider claiming
    `base_url` lives in `pi`. Validating against ENABLED harnesses therefore made
    `module:validate agent_view` fail the moment someone disabled `pi`, which would abort
    `setup:upgrade` before any DB change. Validation looks at INSTALLED providers instead;
    runtime visibility still uses enabled ones, where an unresolvable provider leaves the
    field shown.
    """

    def test_agent_view_still_validates_with_the_declaring_module_disabled(self, tmp_path):
        """Point discovery at a project root whose modules.json DISABLES pi.

        The first version of this test patched `resolve_module_root` at the real repo root,
        where pi is enabled — so the enabled-only code path found the declaration too and
        the test passed with the fix reverted. It proved nothing. A temp root with
        `{"pi": false}` is what makes the enabled/installed distinction observable:
        `iter_module_dirs` always yields the core modules, and only
        `iter_enabled_module_dirs` subtracts the disabled ones.
        """
        import json as _json

        from agento.framework.module_validator import validate_module

        real = REPO / "src" / "agento" / "modules" / "agent_view"
        module = tmp_path / "agent_view"
        module.mkdir()
        (module / "module.json").write_text((real / "module.json").read_text())
        (module / "system.json").write_text((real / "system.json").read_text())

        project_root = tmp_path / "project"
        (project_root / "app" / "etc").mkdir(parents=True)
        (project_root / "app" / "etc" / "modules.json").write_text(
            _json.dumps({"pi": False})
        )

        # Sanity: the disabled-filter really does hide pi at this root, so a regression
        # would be visible here rather than silently passing.
        from agento.framework.module_discovery import (
            iter_enabled_module_dirs,
            iter_module_dirs,
        )
        assert "pi" in {d.name for d in iter_module_dirs(project_root)}
        assert "pi" not in {d.name for d in iter_enabled_module_dirs(project_root)}

        with patch(
            "agento.framework.module_discovery.resolve_module_root",
            return_value=project_root,
        ):
            errors = [e for e in validate_module(module) if "provider_option" in e]
        assert errors == [], (
            "disabling the module that declares a provider option invalidated another "
            f"module's manifest: {errors}"
        )

    def test_visibility_falls_back_to_shown_when_the_provider_cannot_be_resolved(self):
        """The runtime half of the same story: a disabled pi means no declaration is found,
        and the field must stay visible rather than vanish."""
        with patch(
            "agento.framework.harness.provider_options.provider_option_names",
            return_value=frozenset(),
        ), patch(
            "agento.framework.harness.provider_options._provider_exists",
            return_value=False,
        ):
            assert not is_provider_option_hidden(
                BASE_URL_FIELD, harness="pi", provider="ollama",
            )
