"""A job-less run must still name itself to the toolbox.

The toolbox derives the run's desk from what the URL names. Only ``job_id`` was ever
named, so an interactive ``agento run`` landed on ``/workspace/artifacts/_fallback`` —
the ONE path every job-less session shares — and the versioned-artifacts desk tools
refused it with ``WORKSPACE_UNAVAILABLE``. The run already had a unique id (it is the
last segment of its own artifacts dir); it simply never reached the toolbox.

The pipeline tests drive the REAL ``materialize_run_workspace``, because the hole was in
the caller's gate, not in the adapter: a test calling ``inject_runtime_params`` directly
passes while the run is still unscoped.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.harness.run_scope import run_scope_query, scope_toolbox_url
from agento.framework.run_preparation import materialize_run_workspace
from agento.modules.pi.src.config import BRIDGE_CONFIG_FILENAME, BRIDGE_DIR

pytestmark = pytest.mark.usefixtures("builtin_harnesses")


class TestRunScopeQuery:
    def test_a_job_id_wins(self):
        assert run_scope_query(42, "cli-abc") == "job_id=42"

    def test_a_string_run_id_scopes_a_jobless_run(self):
        assert run_scope_query(None, "cli-abc") == "run_id=cli-abc"

    def test_no_id_scopes_nothing(self):
        assert run_scope_query(None, None) == ""

    @pytest.mark.parametrize("run_id", [
        "_fallback",        # the shared desk itself — the one name that must never scope
        "../escape",        # a separator would make one segment into two
        "run.1",            # a dot is how `..` gets in
        "run/1",
        "",
        "x" * 65,
        42,                 # an int run id is a JOB id; it must not arrive as a run id
    ])
    def test_an_unusable_run_id_scopes_nothing(self, run_id):
        assert run_scope_query(None, run_id) == ""

    def test_the_url_keeps_its_existing_query(self):
        url = "http://toolbox:3001/mcp?agent_view_id=7"
        assert scope_toolbox_url(url, None, "cli-abc") == f"{url}&run_id=cli-abc"

    def test_an_unscopeable_run_leaves_the_url_alone(self):
        url = "http://toolbox:3001/mcp?agent_view_id=7"
        assert scope_toolbox_url(url, None, None) == url


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


def _make_build(root: Path) -> None:
    build = root / "build" / "acme" / "dev" / "20260101-000000"
    (build / BRIDGE_DIR).mkdir(parents=True)
    (build / BRIDGE_DIR / BRIDGE_CONFIG_FILENAME).write_text(
        json.dumps({"url": "http://toolbox:3001/mcp?agent_view_id=7"}, indent=2),
    )
    (build / BRIDGE_DIR / "agento-toolbox.js").write_text("// bridge\n")
    (build.parent / "current").symlink_to(build)


def _materialize(tmp_path: Path, *, run_id: int | str):
    runtime = _Runtime(
        agent_view=_AV(code="dev", id=7),
        workspace=_WS(code="acme", id=3),
        harness="pi",
        model="anthropic/claude-sonnet-4.5",
        provider="openrouter",
    )
    with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "artifacts")), \
         patch("agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path / "build")), \
         patch("agento.framework.run_preparation.BUILD_DIR", str(tmp_path / "build")), \
         patch("agento.framework.persistent_home.BUILD_DIR", str(tmp_path / "build")):
        return materialize_run_workspace(runtime, run_id=run_id, em=MagicMock())


def _url(run_dir: Path) -> str:
    return json.loads((run_dir / BRIDGE_DIR / BRIDGE_CONFIG_FILENAME).read_text())["url"]


class TestAJoblessRunIsScopedThroughTheRealPipeline:
    def test_a_string_run_id_reaches_the_toolbox_url(self, tmp_path):
        """The regression: with no override to carry it, injection was skipped entirely
        and the run arrived at the toolbox anonymous."""
        _make_build(tmp_path)
        _, working = _materialize(tmp_path, run_id="cli-abc123")
        assert _url(working).endswith("&run_id=cli-abc123")

    def test_the_scope_names_the_dir_the_run_actually_works_in(self, tmp_path):
        """The run id is the last segment of the artifacts dir, so the desk the toolbox
        builds from the URL is this run's own dir and no one else's."""
        _make_build(tmp_path)
        _, working = _materialize(tmp_path, run_id="cli-abc123")
        assert working.name == "cli-abc123"
        assert f"run_id={working.name}" in _url(working)

    def test_a_job_keeps_its_job_scope_and_gains_no_run_scope(self, tmp_path):
        _make_build(tmp_path)
        _, working = _materialize(tmp_path, run_id=42)
        url = _url(working)
        assert "job_id=42" in url
        assert "run_id" not in url
