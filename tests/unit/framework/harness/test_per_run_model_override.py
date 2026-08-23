"""The per-run model override must reach the bridge's guard on EVERY spawn path.

Pi's bridge fails a run whose assistant messages report a model other than the expected
one — the defence against Pi's silent substring model resolution. The expectation is baked
at BUILD time, so any path that spawns with a per-run ``--model`` must refresh it or the
guard fails the run for doing exactly what was asked.

An earlier fix threaded the override through the consumer's main path only, and a review
reproduced three surviving holes plus a design error. Each test below is one of those
reproductions, driven through the REAL ``materialize_run_workspace`` rather than through
``inject_runtime_params`` directly — the holes were all in the callers, so a test that
calls the adapter itself passes while every one of them is still open.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.run_preparation import materialize_run_workspace
from agento.modules.pi.src.config import (
    BRIDGE_CONFIG_FILENAME,
    BRIDGE_DIR,
    PiWorkspaceAdapter,
)

pytestmark = pytest.mark.usefixtures("builtin_harnesses")

BUILD_MODEL = "anthropic/claude-sonnet-4.5"
RUN_MODEL = "anthropic/claude-haiku-4.5"


@dataclass
class _AV:
    code: str
    id: int


@dataclass
class _WS:
    code: str
    id: int


@dataclass
class _Runtime:
    agent_view: _AV | None
    workspace: _WS | None
    harness: str | None
    model: str | None = None
    provider: str | None = None


class _Svc:
    """Minimal ScopedConfigService stand-in for the no-build fallback."""

    def __init__(self, values: dict[str, str]):
        self._values = values

    def resolve_all(self) -> dict[str, str]:
        return dict(self._values)

    def get(self, path: str):
        return self._values.get(path)


def _runtime(model: str | None = BUILD_MODEL) -> _Runtime:
    return _Runtime(
        agent_view=_AV(code="dev", id=7),
        workspace=_WS(code="acme", id=3),
        harness="pi",
        model=model,
        provider="openrouter",
    )


def _make_build(root: Path, *, expectations: dict[str, object] | None = None) -> Path:
    """A current build carrying a pi connection file, reachable via the `current` symlink."""
    build = root / "build" / "acme" / "dev" / "20260101-000000"
    (build / BRIDGE_DIR).mkdir(parents=True)
    payload: dict[str, object] = {"url": "http://toolbox:3001/mcp?agent_view_id=7"}
    payload.update(expectations if expectations is not None else {"expected_model": BUILD_MODEL})
    (build / BRIDGE_DIR / BRIDGE_CONFIG_FILENAME).write_text(json.dumps(payload, indent=2))
    (build / BRIDGE_DIR / "agento-toolbox.js").write_text("// bridge\n")
    current = build.parent / "current"
    current.symlink_to(build)
    return build


def _connection(run_dir: Path) -> dict:
    return json.loads((run_dir / BRIDGE_DIR / BRIDGE_CONFIG_FILENAME).read_text())


def _materialize(tmp_path: Path, *, run_id: int | str, **kwargs):
    """Drive the real pipeline against tmp_path, with the event manager stubbed.

    ``em`` is injected rather than left ambient: the real
    ``workspace_build_check_before`` observer opens a MySQL connection, so a test that
    relies on the global event manager passes alone and fails once anything else in the
    session has bootstrapped the modules.
    """
    with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "artifacts")), \
         patch("agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path / "build")), \
         patch("agento.framework.run_preparation.BUILD_DIR", str(tmp_path / "build")), \
         patch("agento.framework.persistent_home.BUILD_DIR", str(tmp_path / "build")):
        return materialize_run_workspace(
            _runtime(**kwargs.pop("runtime_kwargs", {})),
            run_id=run_id,
            em=kwargs.pop("em", MagicMock()),
            **kwargs,
        )


class TestStringRunIdsStillGetTheOverride:
    """`agento run` identifies its run by a STRING id, which mapped to `job_id=None`, and
    the framework skipped injection entirely when the job id was absent. So the ONE path an
    operator uses to try a different model interactively was the path that could not."""

    def test_a_string_run_id_applies_the_override(self, tmp_path):
        _make_build(tmp_path)
        _, working = _materialize(tmp_path, run_id="cli-abc123", effective_model=RUN_MODEL)
        assert _connection(working)["expected_model"] == RUN_MODEL

    def test_a_string_run_id_adds_no_job_scope(self, tmp_path):
        """The override must not smuggle in a job id the run does not have — a literal
        `job_id=None` in the URL would be worse than no scope at all."""
        _make_build(tmp_path)
        _, working = _materialize(tmp_path, run_id="cli-abc123", effective_model=RUN_MODEL)
        assert "job_id" not in _connection(working)["url"]

    def test_without_an_override_a_string_run_id_keeps_the_build_expectation(self, tmp_path):
        _make_build(tmp_path)
        _, working = _materialize(tmp_path, run_id="cli-abc123")
        assert _connection(working)["expected_model"] == BUILD_MODEL


class TestIntegerJobIdsKeepBothBehaviours:
    def test_a_job_id_scopes_the_url_and_applies_the_override(self, tmp_path):
        _make_build(tmp_path)
        _, working = _materialize(tmp_path, run_id=42, effective_model=RUN_MODEL)
        conn = _connection(working)
        assert "job_id=42" in conn["url"]
        assert conn["expected_model"] == RUN_MODEL

    def test_a_build_with_no_model_still_gets_a_live_guard(self, tmp_path):
        """The design error: the opt-out used to be inferred from a MISSING
        `expected_model`, so injection refused to create one — and a build whose agent_view
        had no model configured left the guard off even when the run named a model."""
        _make_build(tmp_path, expectations={})
        _, working = _materialize(
            tmp_path, run_id=42, effective_model=RUN_MODEL, runtime_kwargs={"model": None},
        )
        assert _connection(working)["expected_model"] == RUN_MODEL


class TestTheNoBuildFallbackAppliesTheOverride:
    """With no build to inject into, the override has to reach the adapter through the
    config it materializes FROM. This path had no second chance to correct itself."""

    def test_the_fallback_bakes_the_effective_model(self, tmp_path):
        svc = _Svc({"agent_view/model": BUILD_MODEL, "agent_view/provider": "openrouter"})
        _, working = _materialize(
            tmp_path, run_id="cli-xyz", agent_config_svc=svc, effective_model=RUN_MODEL,
        )
        assert _connection(working)["expected_model"] == RUN_MODEL

    def test_the_fallback_without_an_override_uses_the_configured_model(self, tmp_path):
        svc = _Svc({"agent_view/model": BUILD_MODEL, "agent_view/provider": "openrouter"})
        _, working = _materialize(tmp_path, run_id="cli-xyz", agent_config_svc=svc)
        assert _connection(working)["expected_model"] == BUILD_MODEL


class TestTheRouterOptOutSurvivesInjection:
    """A router dispatches to another model by design. The opt-out is now an EXPLICIT
    marker, so refreshing `expected_model` can no longer undo it — which is exactly what
    made creating the key safe."""

    def test_the_marker_survives_a_per_run_override(self, tmp_path):
        _make_build(
            tmp_path,
            expectations={"expected_model": "openrouter/free", "allow_model_substitution": True},
        )
        _, working = _materialize(tmp_path, run_id=42, effective_model=RUN_MODEL)
        conn = _connection(working)
        assert conn["allow_model_substitution"] is True
        assert conn["expected_model"] == RUN_MODEL

    def test_prepare_workspace_writes_the_marker_and_keeps_the_expectation(self, tmp_path):
        PiWorkspaceAdapter().prepare_workspace(
            tmp_path,
            {"provider": "openrouter", "model": "openrouter/free"},
            agent_view_id=7,
            toolbox_url="http://toolbox:3001",
            harness_config={"allow_model_substitution": "1"},
        )
        conn = _connection(tmp_path)
        assert conn["allow_model_substitution"] is True
        # Recorded, not deleted: absence used to mean two different things.
        assert conn["expected_model"] == "openrouter/free"

    def test_without_the_flag_no_marker_is_written(self, tmp_path):
        PiWorkspaceAdapter().prepare_workspace(
            tmp_path,
            {"provider": "openrouter", "model": BUILD_MODEL},
            agent_view_id=7,
            toolbox_url="http://toolbox:3001",
            harness_config={"allow_model_substitution": "0"},
        )
        assert "allow_model_substitution" not in _connection(tmp_path)


class TestSiblingAdaptersAreNeverHandedANoneJobId:
    """The framework calls injection with `job_id=None` only for adapters that declared
    they understand a per-run override. `claude` and `codex` are typed `job_id: int` and
    would render the literal string "None" into their config."""

    @pytest.mark.parametrize("harness", ["claude", "codex"])
    def test_a_strict_adapter_declares_neither_override_nor_varkwargs(self, harness):
        """The gate's premise, asserted against the REAL type.

        A MagicMock is useless here: its signature is `(*args, **kwargs)`, so it reports
        that it accepts `effective_model` and the gate lets `None` through — the double
        would pass while a real strict adapter broke. Construct the real adapter instead.
        """
        import inspect

        from agento.framework.harness import workspace_adapter_for

        params = inspect.signature(workspace_adapter_for(harness).inject_runtime_params).parameters
        assert "effective_model" not in params
        assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())

    def test_a_string_run_id_leaves_a_strict_adapters_config_untouched(self, tmp_path):
        """Behavioural half: claude renders `job_id={job_id}` into every MCP url, so a
        `None` reaching it would appear verbatim as `job_id=None`."""
        from agento.framework.artifacts_dir import copy_build_to_artifacts_dir

        build = tmp_path / "build"
        build.mkdir()
        mcp = {"mcpServers": {"toolbox": {"url": "http://toolbox:3001/mcp"}}}
        (build / ".mcp.json").write_text(json.dumps(mcp))
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        copy_build_to_artifacts_dir(
            build, run_dir, job_id=None, harness="claude", effective_model=RUN_MODEL,
        )
        written = json.loads((run_dir / ".mcp.json").read_text())
        assert written["mcpServers"]["toolbox"]["url"] == "http://toolbox:3001/mcp"

    def test_a_declaring_adapter_is_invoked_with_none(self, tmp_path):
        from agento.framework.artifacts_dir import copy_build_to_artifacts_dir

        build = tmp_path / "build"
        (build / BRIDGE_DIR).mkdir(parents=True)
        (build / BRIDGE_DIR / BRIDGE_CONFIG_FILENAME).write_text(
            json.dumps({"url": "http://toolbox:3001/mcp", "expected_model": BUILD_MODEL})
        )
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        copy_build_to_artifacts_dir(
            build, run_dir, job_id=None, harness="pi", effective_model=RUN_MODEL,
        )
        assert _connection(run_dir)["expected_model"] == RUN_MODEL

    def test_no_override_means_no_call_at_all_for_a_none_job_id(self, tmp_path):
        """Nothing to inject and no job to scope — the call would be pure overhead."""
        from agento.framework.artifacts_dir import copy_build_to_artifacts_dir

        build = tmp_path / "build"
        (build / BRIDGE_DIR).mkdir(parents=True)
        (build / BRIDGE_DIR / BRIDGE_CONFIG_FILENAME).write_text(
            json.dumps({"url": "http://toolbox:3001/mcp", "expected_model": BUILD_MODEL})
        )
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        with patch.object(
            PiWorkspaceAdapter, "inject_runtime_params", autospec=True
        ) as injector:
            copy_build_to_artifacts_dir(build, run_dir, job_id=None, harness="pi")
            injector.assert_not_called()


class TestTheJobLessCallRequiresAnExplicitKeyword:
    """`**kwargs` must NOT be read as "understands a job-less run".

    A reviewer reproduced the hazard: a legacy adapter may carry `**kwargs` purely for
    forward compatibility while still declaring `job_id: int`. The first version of this
    gate treated any VAR_KEYWORD as opt-in and handed such an adapter a `None` it would
    render into its config as the literal "None". Only an explicitly NAMED `effective_*`
    parameter admits the job-less call.
    """

    def _build(self, tmp_path: Path) -> Path:
        build = tmp_path / "build"
        (build / BRIDGE_DIR).mkdir(parents=True)
        (build / BRIDGE_DIR / BRIDGE_CONFIG_FILENAME).write_text(
            json.dumps({"url": "http://toolbox:3001/mcp", "expected_model": BUILD_MODEL})
        )
        return build

    def _call(self, tmp_path, adapter, *, job_id, **kw):
        from agento.framework.artifacts_dir import copy_build_to_artifacts_dir

        build = self._build(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        with patch(
            "agento.framework.harness.workspace_adapter_for", return_value=adapter
        ):
            # A REGISTERED id, because `owned_paths_for` resolves it before the
            # adapter lookup this test substitutes.
            copy_build_to_artifacts_dir(
                build, run_dir, job_id=job_id, harness="pi", **kw
            )
        return run_dir

    def test_a_legacy_kwargs_adapter_is_not_called_with_a_none_job_id(self, tmp_path):
        class LegacyKwargs:
            """`job_id: int` — NOT widened — plus `**kwargs` for compatibility."""

            def __init__(self):
                self.calls = []

            def inject_runtime_params(self, artifacts_dir, *, job_id: int, **kwargs):
                self.calls.append((job_id, kwargs))

        adapter = LegacyKwargs()
        self._call(tmp_path, adapter, job_id=None, effective_model=RUN_MODEL)
        assert adapter.calls == [], (
            "a **kwargs adapter that still declares job_id: int was handed a None job id"
        )

    def test_the_same_adapter_still_receives_overrides_with_a_real_job_id(self, tmp_path):
        """`**kwargs` is enough to RECEIVE an override — it is only the job-less call it
        cannot admit. Narrowing that too would silently drop overrides for such adapters."""
        class LegacyKwargs:
            def __init__(self):
                self.calls = []

            def inject_runtime_params(self, artifacts_dir, *, job_id: int, **kwargs):
                self.calls.append((job_id, kwargs))

        adapter = LegacyKwargs()
        self._call(tmp_path, adapter, job_id=7, effective_model=RUN_MODEL)
        assert adapter.calls == [(7, {"effective_model": RUN_MODEL})]

    def test_an_adapter_declaring_only_effective_provider_is_admitted(self, tmp_path):
        """The gate keyed on `effective_model` alone, so a provider-only adapter was
        excluded from job-less runs even though it declared exactly what was supplied."""
        class ProviderOnly:
            def __init__(self):
                self.calls = []

            def inject_runtime_params(
                self, artifacts_dir, *, job_id: int | None, effective_provider=None,
            ):
                self.calls.append((job_id, effective_provider))

        adapter = ProviderOnly()
        self._call(tmp_path, adapter, job_id=None, effective_provider="openrouter")
        assert adapter.calls == [(None, "openrouter")]

    def test_a_named_model_adapter_is_admitted(self, tmp_path):
        class Modern:
            def __init__(self):
                self.calls = []

            def inject_runtime_params(
                self, artifacts_dir, *, job_id: int | None, effective_model=None,
            ):
                self.calls.append((job_id, effective_model))

        adapter = Modern()
        self._call(tmp_path, adapter, job_id=None, effective_model=RUN_MODEL)
        assert adapter.calls == [(None, RUN_MODEL)]


class TestShippedAdaptersHonourTheWidenedType:
    """The Protocol says `int | None`, so the shipped adapters must actually accept it.

    They are never called with `None` under the gate above, but a Protocol whose own
    implementations contradict it is a lie a third party will read and copy.
    """

    @pytest.mark.parametrize("harness", ["claude", "codex"])
    def test_a_none_job_id_is_a_no_op_rather_than_a_literal_none(self, tmp_path, harness):
        from agento.framework.harness import workspace_adapter_for

        adapter = workspace_adapter_for(harness)
        mcp = tmp_path / ".mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"t": {"url": "http://tb:3001/mcp"}}}))
        toml = tmp_path / ".codex"
        toml.mkdir()
        (toml / "config.toml").write_text('[mcp_servers.toolbox]\nurl = "http://tb:3001/mcp"\n')

        adapter.inject_runtime_params(tmp_path, job_id=None)  # must not raise

        assert "None" not in mcp.read_text()
        assert "None" not in (toml / "config.toml").read_text()
