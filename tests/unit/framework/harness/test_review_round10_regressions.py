"""Regression tests for impl review round 10 — the LAST round of the loop's budget.

These fixes are applied but were NOT re-reviewed. See ESCALATION.md.

Themes: (F1) argv logging is unsound in principle, not just under-redacted — see
``test_review_round9_regressions.TestArgvIsNeverLogged`` for the argv coverage; (F2) `--exec`
ran on the provider default while DISPLAYING the job's model; (F3) codex dropped the Toolbox's
headers and appended a duplicate table on a second call; (F4) framework modules imported from
``framework/cli/``, inverting the layering the extraction existed to fix.
"""
from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.harness import clear
from agento.framework.job_models import AgentType, Job, JobStatus
from tests.harness_fixtures import register_builtin_harnesses

REPO = Path(__file__).resolve().parents[4]
FRAMEWORK = REPO / "src" / "agento" / "framework"


@pytest.fixture(autouse=True)
def _harnesses():
    register_builtin_harnesses()
    yield
    clear()


class TestReplayExecutesTheModelItDisplays:
    """`build_replay_command` resolved `override → job.model → default` correctly, but the
    `--exec` path then passed `args.model` (None without an override), so a replay printed the
    job's model and ran on the provider default — the one thing a replay must not do."""

    def _job(self, model: str | None) -> Job:
        return Job(
            id=7, schedule_id=None, type=AgentType.CRON, source="jira",
            agent_view_id=None, priority=50, reference_id="AI-1",
            agent_type="claude", provider="anthropic", model=model,
            input_tokens=1, output_tokens=1, prompt="p", output=None, context=None,
            idempotency_key="k", status=JobStatus.SUCCESS, attempt=1, max_attempts=3,
            scheduled_after=None, started_at=None, finished_at=None,
            result_summary=None, error_message=None, error_class=None, pid=None,
            session_id=None, created_at=datetime(2026, 1, 1),
            updated_at=datetime(2026, 1, 1),
        )

    def test_resolution_prefers_the_jobs_model_when_no_override(self):
        from agento.framework.replay import build_replay_command

        replay = build_replay_command(self._job("opus-4-1"), model_override=None)
        assert replay.model == "opus-4-1"

    def test_an_explicit_override_wins(self):
        from agento.framework.replay import build_replay_command

        replay = build_replay_command(self._job("opus-4-1"), model_override="sonnet")
        assert replay.model == "sonnet"

    def test_exec_passes_the_resolved_model_not_the_raw_flag(self):
        """The actual bug: `args.model` is None on a replay without `--model`."""
        source = (FRAMEWORK / "cli" / "runtime.py").read_text()
        exec_block = source[source.index("if args.exec:"):]
        exec_block = exec_block[: exec_block.index("print(json.dumps(")]

        assert "model=replay.model" in exec_block, "runner built without the resolved model"
        assert "RunRequest(prompt=replay.prompt, model=replay.model)" in exec_block
        # Comments in that block DOCUMENT the old bug by name, so judge code lines only.
        code = "\n".join(
            line for line in exec_block.splitlines() if not line.lstrip().startswith("#")
        )
        assert "args.model" not in code, "raw flag still reaches execution"

    def test_the_run_context_carries_the_model(self):
        """`_make_runner` omitted it, so even a correct call site could not reach the
        builder — a harness that reads the model off the context would still use its default.
        """
        from agento.framework.cli.runtime import _make_runner

        captured: dict = {}

        def _create_runner(harness, ctx, **kwargs):
            captured["ctx"] = ctx
            return object()

        with (
            patch("agento.framework.harness.create_runner", _create_runner),
            patch(
                "agento.framework.cli.runtime._load_framework_config",
                return_value=(None, MagicMock(disable_llm=False), None),
            ),
        ):
            _make_runner("claude", "anthropic", credential=object(), model="opus-4-1")

        assert captured["ctx"].model == "opus-4-1"

    def test_no_model_stays_none_rather_than_a_substituted_default(self):
        from agento.framework.cli.runtime import _make_runner

        captured: dict = {}
        with (
            patch(
                "agento.framework.harness.create_runner",
                lambda h, ctx, **kw: captured.setdefault("ctx", ctx),
            ),
            patch(
                "agento.framework.cli.runtime._load_framework_config",
                return_value=(None, MagicMock(disable_llm=False), None),
            ),
        ):
            _make_runner("claude", "anthropic", credential=object())

        assert captured["ctx"].model is None


class TestToolboxConnectionIsSerializedFaithfully:
    """`ToolboxConnectionSpec` carries `headers` (the Toolbox's auth on deployments that set
    one) and codex wrote only transport + url — a silent auth loss. It also APPENDED the table,
    so a second call left two `[mcp_servers.toolbox]` tables and codex takes the last one."""

    def _adapter(self):
        from agento.framework.harness import workspace_adapter_for

        return workspace_adapter_for("codex")

    def _spec(self, **kw):
        from agento.framework.harness.runtime import ToolboxConnectionSpec

        return ToolboxConnectionSpec(**{
            "name": "toolbox", "transport": "http",
            "url": "http://toolbox:3000/mcp", "headers": {}, **kw,
        })

    def _parsed(self, path: Path) -> dict:
        import tomllib

        return tomllib.loads(path.read_text())

    def test_headers_are_written(self, tmp_path):
        self._adapter().serialize_toolbox_connection(
            self._spec(headers={"Authorization": "Bearer tok"}), tmp_path,
        )

        table = self._parsed(tmp_path / ".codex" / "config.toml")["mcp_servers"]["toolbox"]
        assert table["http_headers"] == {"Authorization": "Bearer tok"}
        assert table["url"] == "http://toolbox:3000/mcp"
        assert table["type"] == "http"

    def test_no_headers_emits_no_key(self, tmp_path):
        self._adapter().serialize_toolbox_connection(self._spec(), tmp_path)

        table = self._parsed(tmp_path / ".codex" / "config.toml")["mcp_servers"]["toolbox"]
        assert "http_headers" not in table

    def test_two_calls_leave_exactly_one_table(self, tmp_path):
        adapter = self._adapter()
        adapter.serialize_toolbox_connection(self._spec(url="http://old/mcp"), tmp_path)
        adapter.serialize_toolbox_connection(self._spec(url="http://new/mcp"), tmp_path)

        text = (tmp_path / ".codex" / "config.toml").read_text()
        assert text.count("[mcp_servers.toolbox]") == 1
        # tomllib REJECTS a duplicate table outright, so parsing is itself the assertion.
        assert self._parsed(
            tmp_path / ".codex" / "config.toml",
        )["mcp_servers"]["toolbox"]["url"] == "http://new/mcp"

    def test_unrelated_config_survives(self, tmp_path):
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "config.toml").write_text(
            'model = "gpt-5"\n\n[mcp_servers.other]\ntype = "stdio"\n',
        )

        self._adapter().serialize_toolbox_connection(self._spec(), tmp_path)

        parsed = self._parsed(tmp_path / ".codex" / "config.toml")
        assert parsed["model"] == "gpt-5"
        assert parsed["mcp_servers"]["other"]["type"] == "stdio"
        assert "toolbox" in parsed["mcp_servers"]

    def test_a_second_call_after_headers_replaces_them(self, tmp_path):
        """Rotating away from an authenticated Toolbox must not leave the old header."""
        adapter = self._adapter()
        adapter.serialize_toolbox_connection(
            self._spec(headers={"Authorization": "Bearer old"}), tmp_path,
        )
        adapter.serialize_toolbox_connection(self._spec(), tmp_path)

        assert "Bearer old" not in (tmp_path / ".codex" / "config.toml").read_text()


class TestFrameworkDoesNotImportFromTheCliLayer:
    """`module_discovery` was extracted from `cli/_provisioning` precisely so framework
    contracts would not depend on the CLI layer — and then imported `cli._project`.
    `config_dependents` did the same with `cli.config`. Enforced structurally: a grep-style
    guard is the only thing that keeps the layering from drifting back."""

    def _guarded_modules(self) -> list[Path]:
        """The contract surface: the harness package plus the two modules F4 named.

        Deliberately NOT every framework module — `e2e.py` and `setup.py` are CLI-adjacent
        entry points that legitimately drive `cli.runtime`/`cli.terminal`, and widening this
        guard to them is a separate refactor, not this diff's scope.
        """
        return [
            *(FRAMEWORK / "harness").rglob("*.py"),
            FRAMEWORK / "module_discovery.py",
            FRAMEWORK / "config_dependents.py",
            FRAMEWORK / "config_schema_options.py",
            FRAMEWORK / "project.py",
        ]

    def test_no_guarded_framework_module_imports_framework_cli(self):
        offenders = []
        for path in self._guarded_modules():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and (
                    node.module.startswith("cli.") or node.module == "cli"
                    or ".cli." in f".{node.module}."
                ):
                    offenders.append(f"{path.relative_to(REPO)}: {node.module}")
                if isinstance(node, ast.ImportFrom) and node.level and node.module is None:
                    names = {a.name for a in node.names}
                    if "cli" in names:
                        offenders.append(f"{path.relative_to(REPO)}: relative cli")
        assert offenders == [], offenders

    def test_project_root_lives_in_the_framework(self):
        from agento.framework.project import find_project_root

        assert find_project_root(REPO) == REPO

    def test_the_cli_still_re_exports_it_for_existing_callers(self):
        from agento.framework.cli._project import find_project_root as cli_fn
        from agento.framework.project import find_project_root as fw_fn

        assert cli_fn is fw_fn

    def test_field_options_lives_in_the_framework(self):
        from agento.framework.config_schema_options import field_options

        assert field_options({"options": [{"value": "a", "label": "A"}]}) == [
            {"value": "a", "label": "A"},
        ]

    def test_the_cli_and_admin_consume_the_framework_module(self):
        cli = (FRAMEWORK / "cli" / "config.py").read_text()
        admin = (FRAMEWORK / "admin" / "data.py").read_text()

        assert "from ..config_schema_options import field_options" in cli
        assert "from ..config_schema_options import field_options" in admin
        assert "from ..cli.config import field_options" not in admin

    def test_a_malformed_field_def_yields_no_options(self):
        from agento.framework.config_schema_options import field_options

        assert field_options({}) == []
        assert field_options({"options": ["not-a-dict", {"label": "no value"}]}) == []
