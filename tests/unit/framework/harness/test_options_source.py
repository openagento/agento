"""``options_source`` lets a select field enumerate harnesses that don't exist yet.

The alternative — a static ``options`` array in ``system.json`` — would have to be edited
in the framework every time a harness is added, which is the closed-enum problem again.
Resolution reads declarations OFF DISK because ``config:set`` validates a value with no
``bootstrap()`` and no Python import available.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.framework.harness import SUPPORTED_SOURCES, resolve_options


def _write_harness_module(root: Path, name: str, harness_id: str, providers: list[dict]) -> Path:
    mod = root / "app" / "code" / name
    mod.mkdir(parents=True)
    (mod / "module.json").write_text(json.dumps({"name": name, "version": "0.1.0"}))
    (mod / "di.json").write_text(json.dumps({"agent_harnesses": [{
        "id": harness_id,
        "label": harness_id.title(),
        "class": "src.adapter.A",
        "default_provider": providers[0]["id"],
        "providers": providers,
    }]}))
    return root


class TestHarnessRegistryOptions:
    def test_core_harnesses_are_offered(self):
        values = {o["value"] for o in resolve_options("agent_harness_registry")}
        assert {"claude", "codex"} <= values

    def test_a_new_module_appears_without_touching_the_framework(self, tmp_path):
        proj = _write_harness_module(
            tmp_path, "hermes", "hermes",
            [{"id": "local", "label": "Local", "credential_required": False}],
        )
        options = resolve_options("agent_harness_registry", project_root=proj)
        assert {"value": "hermes", "label": "Hermes"} in options

    def test_labels_come_from_the_declaration(self):
        options = resolve_options("agent_harness_registry")
        by_value = {o["value"]: o["label"] for o in options}
        assert by_value["codex"] == "OpenAI Codex"


class TestProviderOptions:
    def test_providers_are_filtered_by_the_selected_harness(self):
        options = resolve_options("agent_harness_providers", depends_on_value="codex")
        assert [o["value"] for o in options] == ["openai"]

    def test_without_a_selection_every_provider_is_offered_prefixed(self):
        """Usable before a harness has been picked — prefixed so ambiguous provider ids
        stay distinguishable in the picker."""
        options = resolve_options("agent_harness_providers")
        labels = {o["label"] for o in options}
        assert any(label.startswith("OpenAI Codex: ") for label in labels)
        assert {"anthropic", "openai"} <= {o["value"] for o in options}

    def test_a_harness_with_two_providers_offers_both(self, tmp_path):
        proj = _write_harness_module(
            tmp_path, "hermes", "hermes",
            [
                {"id": "local", "label": "Local", "credential_required": False},
                {"id": "cloud", "label": "Cloud", "credential_required": True,
                 "credential_scope": "hermes_cloud", "registration_modes": ["api_key"]},
            ],
        )
        options = resolve_options(
            "agent_harness_providers", project_root=proj, depends_on_value="hermes",
        )
        assert [o["value"] for o in options] == ["local", "cloud"]

    def test_unknown_harness_selection_yields_no_providers(self):
        assert resolve_options("agent_harness_providers", depends_on_value="nope") == []


class TestSourceValidation:
    def test_unknown_source_is_rejected_and_names_the_supported_ones(self):
        with pytest.raises(ValueError, match="Unknown options_source"):
            resolve_options("agent_something_else")

    def test_supported_sources_are_exactly_the_two_declared(self):
        assert set(SUPPORTED_SOURCES) == {
            "agent_harness_registry", "agent_harness_providers",
        }

    def test_agent_view_system_json_uses_only_supported_sources(self):
        """A typo in ``system.json`` must be caught by ``module:validate``, not at runtime
        when an operator opens the config picker."""
        system = Path("src/agento/modules/agent_view/system.json")
        fields = json.loads(system.read_text())
        sources = {
            f["options_source"] for f in fields.values() if "options_source" in f
        }
        assert sources <= set(SUPPORTED_SOURCES)

    def test_provider_field_declares_its_dependency_on_harness(self):
        system = Path("src/agento/modules/agent_view/system.json")
        fields = json.loads(system.read_text())
        assert fields["provider"]["depends_on"] == "agent_view/harness"
