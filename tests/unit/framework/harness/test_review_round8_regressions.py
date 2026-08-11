"""Regression tests for impl review round 8 (final round of the loop's budget).

These fixes are applied but were NOT re-reviewed — the loop's 8-round budget is spent. See
ESCALATION.md.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agento.framework.harness import RunRequest, clear, workspace_adapter_for
from tests.harness_fixtures import make_runner, register_builtin_harnesses

REPO = Path(__file__).resolve().parents[4]

OUTPUT_SENTINEL = "PARTIAL-AGENT-OUTPUT-3d7e"


class TestCredentialFreeRunIsReallyCredentialFree:
    """A run dir is COPIED from the current build, which may already hold credentials a
    previous `materialize_agent_credentials` wrote. So proceeding with `credential=None`
    (empty pool → operator wants `/login`) silently inherited a possibly disabled, errored
    or deregistered credential. "Not writing a credential" is not the same as "no credential
    present"."""

    @pytest.fixture(autouse=True)
    def _harnesses(self):
        register_builtin_harnesses()
        yield
        clear()

    def test_remove_credentials_is_part_of_the_protocol(self):
        from agento.framework.harness.protocols import WorkspaceAdapter

        assert hasattr(WorkspaceAdapter, "remove_credentials")

    def test_claude_removes_login_state_but_keeps_config(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        creds = tmp_path / ".claude" / ".credentials.json"
        creds.write_text('{"claudeAiOauth": {"refreshToken": "stale"}}')
        config = tmp_path / ".claude.json"
        config.write_text('{"model": "opus", "mcpServers": {"toolbox": {}}}')

        workspace_adapter_for("claude").remove_credentials(tmp_path)

        assert not creds.exists(), "stale credential survived"
        # Config must survive — a login against an unconfigured agent is useless.
        assert json.loads(config.read_text())["model"] == "opus"

    def test_codex_removes_auth_but_keeps_config_toml(self, tmp_path):
        (tmp_path / ".codex").mkdir()
        auth = tmp_path / ".codex" / "auth.json"
        auth.write_text('{"tokens": {"refresh_token": "stale"}}')
        config = tmp_path / ".codex" / "config.toml"
        config.write_text('model = "o3"\n')

        workspace_adapter_for("codex").remove_credentials(tmp_path)

        assert not auth.exists()
        assert 'model = "o3"' in config.read_text()

    def test_remove_is_idempotent_on_a_clean_dir(self, tmp_path):
        for harness in ("claude", "codex"):
            target = tmp_path / harness
            target.mkdir()
            workspace_adapter_for(harness).remove_credentials(target)  # must not raise

    def test_materialize_purges_when_asked(self, tmp_path, monkeypatch):
        """The integration point: purge_credentials=True with no credential must clear the
        copied build's credential state."""
        from dataclasses import dataclass

        from agento.framework.run_preparation import materialize_run_workspace

        @dataclass
        class _AV:
            code: str
            id: int

        @dataclass
        class _Runtime:
            agent_view: _AV
            workspace: _AV
            harness: str

        stale = tmp_path / "artifacts" / "acme" / "dev" / "run"
        removed: list[Path] = []
        writer = MagicMock()
        writer.owned_paths.return_value = (set(), set())
        writer.persistent_home_paths.return_value = []
        writer.remove_credentials.side_effect = removed.append

        monkeypatch.setattr(
            "agento.framework.harness.workspace_adapter_for", lambda _h: writer,
        )
        monkeypatch.setattr(
            "agento.framework.harness.registry.workspace_adapter_for", lambda _h: writer,
        )
        monkeypatch.setattr(
            "agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "artifacts"),
        )
        monkeypatch.setattr(
            "agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path / "build"),
        )

        materialize_run_workspace(
            _Runtime(_AV("dev", 7), _AV("acme", 3), "claude"),
            run_id="run", em=MagicMock(), credential=None, purge_credentials=True,
        )

        assert removed, "purge_credentials=True did not remove credential state"
        writer.write_credentials.assert_not_called()
        assert stale.parent.exists()

    def test_materialize_does_not_purge_by_default(self, tmp_path, monkeypatch):
        """The consumer path must be untouched: it always has a credential when required."""
        from dataclasses import dataclass

        from agento.framework.run_preparation import materialize_run_workspace

        @dataclass
        class _AV:
            code: str
            id: int

        @dataclass
        class _Runtime:
            agent_view: _AV
            workspace: _AV
            harness: str

        writer = MagicMock()
        writer.owned_paths.return_value = (set(), set())
        writer.persistent_home_paths.return_value = []
        monkeypatch.setattr(
            "agento.framework.harness.workspace_adapter_for", lambda _h: writer,
        )
        monkeypatch.setattr(
            "agento.framework.harness.registry.workspace_adapter_for", lambda _h: writer,
        )
        monkeypatch.setattr(
            "agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "a"),
        )
        monkeypatch.setattr(
            "agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path / "b"),
        )

        materialize_run_workspace(
            _Runtime(_AV("dev", 7), _AV("acme", 3), "claude"),
            run_id="run", em=MagicMock(), credential=None,
        )

        writer.remove_credentials.assert_not_called()

    def test_prepare_run_purges_only_on_the_interactive_fallback(self):
        from agento.modules.agent_view.src.commands import prepare_run

        source = Path(prepare_run.__file__).read_text()
        assert "purge_credentials = True" in source
        assert "purge_credentials=purge_credentials" in source


class TestEveryRunnerFailureCarriesItsOutput:
    """Round 7 routed failure output to `job.output` via `error.agent_output`, but only from
    the rc!=0 branch. Timeouts and parser-classified failures (auth, usage limit) are raised
    earlier, so those — the most common real failures — still left `job.output` empty."""

    @pytest.fixture(autouse=True)
    def _harnesses(self):
        register_builtin_harnesses()
        yield
        clear()

    def test_timeout_carries_partial_output(self):
        runner = make_runner("claude", credential=None, credential_required=False)
        runner._record_usage = MagicMock()

        def _timeout(cmd, env):
            exc = subprocess.TimeoutExpired(cmd="claude", timeout=1, output=OUTPUT_SENTINEL)
            exc.session_id = None
            exc.agent_output = OUTPUT_SENTINEL
            raise exc

        runner._execute_process = _timeout
        with pytest.raises(subprocess.TimeoutExpired) as exc:
            runner.execute(RunRequest(prompt="p"))

        assert getattr(exc.value, "agent_output", None) == OUTPUT_SENTINEL

    def test_timeout_path_attaches_it_in_source(self):
        """The construction site is inside _execute_process, which the test above stubs."""
        from agento.framework.harness import subprocess_runner

        source = Path(subprocess_runner.__file__).read_text()
        timeout_block = source[source.index("if timed_out:"):]
        timeout_block = timeout_block[: timeout_block.index("raise exc")]
        # Round 9: stdout alone persisted "" for a stderr-only harness, so this now goes
        # through the stream-aware helper.
        assert "exc.agent_output = self._failure_output(stdout, stderr)" in timeout_block

    def test_classified_parser_failure_carries_output(self):
        """An AuthenticationError raised by the parser must not lose the output."""
        runner = make_runner("claude", credential=None, credential_required=False)
        runner._record_usage = MagicMock()
        runner._execute_process = MagicMock(
            return_value=MagicMock(returncode=0, stdout=OUTPUT_SENTINEL, stderr=""),
        )

        from agento.framework.agent_manager.errors import AuthenticationError

        def _boom(_raw):
            raise AuthenticationError("401 invalid credentials")

        runner._parse_output = _boom

        with pytest.raises(AuthenticationError) as exc:
            runner.execute(RunRequest(prompt="p"))

        assert OUTPUT_SENTINEL in getattr(exc.value, "agent_output", "")

    def test_an_already_attached_output_is_not_overwritten(self):
        runner = make_runner("claude", credential=None, credential_required=False)
        runner._record_usage = MagicMock()
        runner._execute_process = MagicMock(
            return_value=MagicMock(returncode=0, stdout="raw", stderr=""),
        )

        def _boom(_raw):
            exc = RuntimeError("classified")
            exc.agent_output = "already-set"
            raise exc

        runner._parse_output = _boom
        with pytest.raises(RuntimeError) as exc:
            runner.execute(RunRequest(prompt="p"))

        assert exc.value.agent_output == "already-set"


class TestDependsOnValidatedForLiteralOptions:
    """`depends_on` was only checked inside the `options_source` branch, so a literal-options
    select with a dangling dependency passed `module:validate` and silently narrowed to
    nothing at runtime."""

    def _module(self, tmp_path, field: dict) -> Path:
        (tmp_path / "module.json").write_text(json.dumps({
            "name": "m", "version": "1.0.0", "description": "d",
        }))
        (tmp_path / "system.json").write_text(json.dumps({"f": field}))
        return tmp_path

    def test_dangling_dependency_on_a_literal_select_is_an_error(self, tmp_path):
        from agento.framework.module_validator import validate_module

        errors = validate_module(self._module(tmp_path, {
            "type": "select", "label": "F",
            "options": [{"value": "a", "label": "A"}],
            "depends_on": "m/ghost",
        }))

        assert any("depends_on 'm/ghost'" in e for e in errors), errors

    def test_valid_dependency_passes(self, tmp_path):
        from agento.framework.module_validator import validate_module

        (tmp_path / "module.json").write_text(json.dumps({
            "name": "m", "version": "1.0.0", "description": "d",
        }))
        (tmp_path / "system.json").write_text(json.dumps({
            "parent": {"type": "string", "label": "P"},
            "f": {
                "type": "select", "label": "F",
                "options": [{"value": "a", "label": "A"}],
                "depends_on": "m/parent",
            },
        }))

        assert validate_module(tmp_path) == []

    def test_options_source_dependency_still_validated(self, tmp_path):
        """The original branch must keep working."""
        from agento.framework.module_validator import validate_module

        errors = validate_module(self._module(tmp_path, {
            "type": "select", "label": "F",
            "options_source": "agent_harness_providers",
            "depends_on": "m/ghost",
        }))

        assert any("depends_on 'm/ghost'" in e for e in errors), errors


class TestOperatorDocsRenamed:
    def test_readme_and_admin_docs_say_credentials(self):
        """The dead ACTION must be gone. Prose documenting its ABSENCE is correct docs, so
        this uses the same narrowed rule as the drift guard rather than banning the words."""
        import re

        dead_action = re.compile(r"[Ss]et\b[^.\n]{0,40}\btoken\b[^.\n]{0,25}\bprimary\b")
        for rel in ("README.md", "docs/cli/admin.md"):
            text = (REPO / rel).read_text()
            assert not dead_action.search(text), rel
            assert "tokens, agent views" not in text, rel

    def test_getting_started_uses_the_real_commands(self):
        text = (REPO / "docs" / "getting-started.md").read_text()

        assert "agento token register" not in text
        assert "agento credential:register claude my-token" in text
        # And points at both config paths, since the harness alone is not enough.
        assert "agent_view/harness" in text and "agent_view/provider" in text
