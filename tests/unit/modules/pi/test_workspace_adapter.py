"""Pi workspace materialization: bridge install, job scoping, sessions, credentials."""
from __future__ import annotations

import json

import pytest

from agento.framework.agent_manager.models import CredentialRecord
from agento.framework.harness import ToolboxConnectionSpec
from agento.modules.pi.src.config import PiWorkspaceAdapter


@pytest.fixture
def adapter():
    return PiWorkspaceAdapter()


def read_conn(root):
    return json.loads((root / ".pi" / "agento-toolbox.json").read_text())


class TestPrepareWorkspace:
    def test_writes_settings_and_installs_the_bridge(self, adapter, tmp_path):
        adapter.prepare_workspace(tmp_path, {}, agent_view_id=7, toolbox_url="http://toolbox:3001")
        settings = json.loads((tmp_path / ".pi" / "agent" / "settings.json").read_text())
        # Non-interactive runs never see a trust prompt, and an untrusted project's
        # config is IGNORED — so the config lives in the per-run HOME and says trusted.
        assert settings["defaultProjectTrust"] == "trusted"
        bridge = tmp_path / ".pi" / "agento-toolbox.js"
        assert bridge.is_file() and bridge.stat().st_size > 0

    def test_does_not_write_AGENTS_md(self, adapter, tmp_path):
        """workspace_build already writes it to the build dir, which is Pi's cwd. A
        second copy in the global home would CONCATENATE, duplicating every instruction
        in every job."""
        adapter.prepare_workspace(tmp_path, {}, agent_view_id=1, toolbox_url="http://tb:3001")
        assert not (tmp_path / ".pi" / "agent" / "AGENTS.md").exists()

    def test_scopes_the_url_to_the_agent_view(self, adapter, tmp_path):
        adapter.prepare_workspace(tmp_path, {}, agent_view_id=7, toolbox_url="http://toolbox:3001")
        assert read_conn(tmp_path)["url"] == "http://toolbox:3001/mcp?agent_view_id=7"

    def test_no_agent_view_leaves_the_url_unscoped(self, adapter, tmp_path):
        adapter.prepare_workspace(tmp_path, {}, agent_view_id=None, toolbox_url="http://toolbox:3001/")
        assert read_conn(tmp_path)["url"] == "http://toolbox:3001/mcp"


class TestOllamaCatalogue:
    def test_absent_for_a_hosted_provider(self, adapter, tmp_path):
        adapter.prepare_workspace(
            tmp_path, {"provider": "openrouter", "model": "m"}, agent_view_id=1,
            toolbox_url="http://tb:3001",
        )
        assert not (tmp_path / ".pi" / "agent" / "models.json").exists()

    def test_generated_for_ollama_with_an_exact_model_id(self, adapter, tmp_path):
        """The id must equal agent_view/model EXACTLY, or Pi takes its substring-matching
        path and clones a fallback model carrying another model's context window,
        token limits and pricing."""
        adapter.prepare_workspace(
            tmp_path,
            {
                "provider": "ollama",
                "model": "qwen2.5-coder:32b",
                "provider_options/base_url": "http://ollama:11434/v1",
            },
            agent_view_id=1,
            toolbox_url="http://tb:3001",
        )
        cat = json.loads((tmp_path / ".pi" / "agent" / "models.json").read_text())
        provider = cat["providers"]["ollama"]
        assert provider["baseUrl"] == "http://ollama:11434/v1"
        assert len(provider["models"]) >= 1, "a provider with no models is 'Unknown provider'"
        assert provider["models"][0]["id"] == "qwen2.5-coder:32b"
        assert provider["compat"] == {
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": False,
        }
        assert "authHeader" not in provider, "authHeader throws when no key is present"

    def test_carries_the_placeholder_api_key_pi_demands(self, adapter, tmp_path):
        """Ollama needs no auth, but Pi hides a model whose provider has no key.

        Without this the run exits before reaching the model with "No API key found for
        ollama." — the whole credential-free provider was unusable, verified live. Pi's
        own models.md prescribes a dummy value for keyless local servers. It is a
        placeholder, never a credential: the value is fixed in code and the server
        ignores it, so nothing here can leak a secret.
        """
        from agento.modules.pi.src.config import OLLAMA_PLACEHOLDER_API_KEY

        adapter.prepare_workspace(
            tmp_path,
            {
                "provider": "ollama",
                "model": "qwen2.5:0.5b",
                "provider_options/base_url": "http://ollama:11434/v1",
            },
            agent_view_id=1,
            toolbox_url="http://tb:3001",
        )
        provider = json.loads(
            (tmp_path / ".pi" / "agent" / "models.json").read_text()
        )["providers"]["ollama"]
        assert provider["apiKey"] == OLLAMA_PLACEHOLDER_API_KEY
        assert provider["apiKey"], "an empty key is the same failure as no key"


class TestModelExpectations:
    """The bridge's in-process guard is dead code unless these keys are written."""

    def test_written_from_the_agent_view_config(self, adapter, tmp_path):
        adapter.prepare_workspace(
            tmp_path, {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.5"},
            agent_view_id=1, toolbox_url="http://tb:3001",
        )
        payload = read_conn(tmp_path)
        assert payload["expected_provider"] == "openrouter"
        assert payload["expected_model"] == "anthropic/claude-sonnet-4.5"

    def test_absent_when_unconfigured_so_the_guard_stays_off(self, adapter, tmp_path):
        adapter.prepare_workspace(tmp_path, {}, agent_view_id=1, toolbox_url="http://tb:3001")
        payload = read_conn(tmp_path)
        assert "expected_model" not in payload
        assert "expected_provider" not in payload

    def test_blank_values_are_not_written(self, adapter, tmp_path):
        adapter.prepare_workspace(
            tmp_path, {"provider": "  ", "model": ""}, agent_view_id=1,
            toolbox_url="http://tb:3001",
        )
        assert "expected_provider" not in read_conn(tmp_path)


class TestJobScoping:
    def test_inject_runtime_params_appends_the_job_id(self, adapter, tmp_path):
        adapter.prepare_workspace(tmp_path, {}, agent_view_id=7, toolbox_url="http://toolbox:3001")
        adapter.inject_runtime_params(tmp_path, job_id=42)
        assert read_conn(tmp_path)["url"].endswith("?agent_view_id=7&job_id=42")

    def test_uses_a_question_mark_when_there_is_no_query_yet(self, adapter, tmp_path):
        adapter.prepare_workspace(tmp_path, {}, agent_view_id=None, toolbox_url="http://tb:3001")
        adapter.inject_runtime_params(tmp_path, job_id=9)
        assert read_conn(tmp_path)["url"] == "http://tb:3001/mcp?job_id=9"

    def test_is_a_no_op_when_the_file_is_absent(self, adapter, tmp_path):
        adapter.inject_runtime_params(tmp_path, job_id=1)  # must not raise


class TestSerializeToolboxConnection:
    def test_writes_url_and_headers(self, adapter, tmp_path):
        spec = ToolboxConnectionSpec(
            name="toolbox", transport="http", url="http://tb:3001/mcp", headers={"X-A": "1"}
        )
        adapter.serialize_toolbox_connection(spec, tmp_path)
        payload = read_conn(tmp_path)
        assert payload["url"] == "http://tb:3001/mcp"
        assert payload["headers"] == {"X-A": "1"}


class TestOwnershipAndPersistence:
    def test_owns_the_bridge_and_its_config(self, adapter):
        files, dirs = adapter.owned_paths()
        assert ".pi/agento-toolbox.js" in files
        assert ".pi/agento-toolbox.json" in files
        assert ".pi" in dirs

    def test_sessions_must_survive_the_run_dir_rmtree(self, adapter):
        """prepare_artifacts_dir rmtree's the run directory on EVERY attempt, so without
        this the session a resume needs is deleted before the resume happens."""
        assert adapter.persistent_home_paths() == [".pi/agent/sessions"]


def credential(cred_type: str, payload: dict) -> CredentialRecord:
    """A REAL CredentialRecord.

    An earlier version of this file used a hand-built fake exposing
    ``credential_type``. `CredentialRecord` has no such attribute — the column is
    ``type`` — so the fake made a broken adapter look correct: `credential_env` returned
    `{}` in production and OPENROUTER_API_KEY never reached Pi. Construct the real record.
    """
    return CredentialRecord(id=1, scope="openrouter", type=cred_type, label="t",
                            credentials=payload)


class TestCredentials:
    def test_openrouter_key_goes_to_the_environment(self, adapter):
        cred = credential("openrouter_api_key", {"api_key": "sk-or-123"})
        assert adapter.credential_env(cred) == {"OPENROUTER_API_KEY": "sk-or-123"}

    def test_the_discriminator_is_the_real_column_name(self, adapter):
        """Guards the exact defect above: `.type`, not `.credential_type`."""
        cred = credential("openrouter_api_key", {"api_key": "k"})
        assert hasattr(cred, "type")
        assert not hasattr(cred, "credential_type")
        assert adapter.credential_env(cred)["OPENROUTER_API_KEY"] == "k"

    def test_another_credential_type_yields_nothing(self, adapter):
        assert adapter.credential_env(credential("claude_oauth", {"api_key": "x"})) == {}

    def test_a_credential_with_no_key_yields_nothing(self, adapter):
        assert adapter.credential_env(credential("openrouter_api_key", {})) == {}

    def test_write_credentials_never_touches_disk(self, adapter, tmp_path):
        adapter.write_credentials(tmp_path, credential("openrouter_api_key", {"api_key": "k"}))
        assert list(tmp_path.iterdir()) == []

    def test_remove_credentials_deletes_auth_json_only(self, adapter, tmp_path):
        pi_home = tmp_path / ".pi" / "agent"
        pi_home.mkdir(parents=True)
        (pi_home / "auth.json").write_text("{}")
        (pi_home / "auth.json.lock").write_text("")
        (pi_home / "settings.json").write_text("{}")
        (pi_home / "models.json").write_text("{}")

        adapter.remove_credentials(tmp_path)

        assert not (pi_home / "auth.json").exists()
        assert not (pi_home / "auth.json.lock").exists()
        assert (pi_home / "settings.json").exists(), "config must survive"
        assert (pi_home / "models.json").exists(), "config must survive"

    def test_nothing_to_capture_for_a_non_rotating_key(self, adapter, tmp_path):
        """Called with the PROTOCOL's three-argument shape, which the consumer uses after
        every credentialed run. A one-argument version raised TypeError on every
        successful Pi job."""
        cred = credential("openrouter_api_key", {"api_key": "k"})
        assert adapter.capture_refreshed_credentials(tmp_path, cred, object()) is False

    def test_the_capture_signature_matches_the_protocol(self, adapter):
        import inspect

        from agento.framework.harness.protocols import WorkspaceAdapter

        expected = inspect.signature(WorkspaceAdapter.capture_refreshed_credentials)
        actual = inspect.signature(type(adapter).capture_refreshed_credentials)
        assert list(actual.parameters) == list(expected.parameters), (
            "Python Protocols do not check signatures, so a mismatch only surfaces as a "
            "TypeError at runtime — assert it here instead"
        )


class TestPerRunModelOverride:
    """A per-run `--model` override must not be failed by a stale build-time expectation.

    `prepare_workspace` writes expectations from the agent_view config at BUILD time, but
    the consumer supports `model_override` per run. Without correcting the expectation, a
    legitimate override would trip the bridge's guard.
    """

    def test_effective_model_replaces_the_build_time_expectation(self, adapter, tmp_path):
        adapter.prepare_workspace(
            tmp_path, {"provider": "openrouter", "model": "build/time"},
            agent_view_id=1, toolbox_url="http://tb:3001",
        )
        adapter.inject_runtime_params(
            tmp_path, job_id=5, effective_model="run/time", effective_provider="openrouter"
        )
        payload = read_conn(tmp_path)
        assert payload["expected_model"] == "run/time"
        assert payload["url"].endswith("job_id=5")

    def test_omitting_the_override_keeps_the_build_time_value(self, adapter, tmp_path):
        adapter.prepare_workspace(
            tmp_path, {"provider": "openrouter", "model": "build/time"},
            agent_view_id=1, toolbox_url="http://tb:3001",
        )
        adapter.inject_runtime_params(tmp_path, job_id=5)
        assert read_conn(tmp_path)["expected_model"] == "build/time"


class TestRouterModelOptOut:
    """`pi/allow_model_substitution=1` must disable ONLY the model check.

    Found live: `openrouter/free` is a router (`architecture.tokenizer == "Router"`) and
    dispatched to `poolside/laguna-xs-2.1:free`, which the identity guard correctly flagged
    — a false positive on a documented configuration. The provider check must survive, or
    the opt-out would disable more than it should.

    The opt-out is an EXPLICIT marker, not the ABSENCE of `expected_model`. An earlier
    version deleted the key, and a review then reproduced the consequence: a build with no
    model configured is indistinguishable from a router opt-out, so a run that named a
    model explicitly still got no guard.
    """

    def test_marks_the_opt_out_without_dropping_the_expectation(self, adapter, tmp_path):
        adapter.prepare_workspace(
            tmp_path,
            {"provider": "openrouter", "model": "openrouter/free"},
            agent_view_id=1,
            toolbox_url="http://tb:3001",
            harness_config={"allow_model_substitution": "1"},
        )
        payload = read_conn(tmp_path)
        assert payload["allow_model_substitution"] is True
        # Kept, so the file still records what was configured; the marker, not absence,
        # is what tells the bridge not to enforce it.
        assert payload["expected_model"] == "openrouter/free"
        assert payload["expected_provider"] == "openrouter"

    def test_without_the_flag_the_model_expectation_is_written(self, adapter, tmp_path):
        adapter.prepare_workspace(
            tmp_path,
            {"provider": "openrouter", "model": "openrouter/free"},
            agent_view_id=1,
            toolbox_url="http://tb:3001",
            harness_config={"allow_model_substitution": "0"},
        )
        assert read_conn(tmp_path)["expected_model"] == "openrouter/free"

    def test_absent_harness_config_behaves_as_off(self, adapter, tmp_path):
        adapter.prepare_workspace(
            tmp_path, {"provider": "openrouter", "model": "m"},
            agent_view_id=1, toolbox_url="http://tb:3001",
        )
        payload = read_conn(tmp_path)
        assert payload["expected_model"] == "m"
        # No marker at all rather than an explicit `false`: the bridge defaults to
        # enforcing, so a build predating the key behaves exactly as it did.
        assert "allow_model_substitution" not in payload
