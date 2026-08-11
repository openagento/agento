"""Integration tests: agent_view module writes instruction files into run directories.

Uses real MySQL + mocked Claude runner. Tests the full observer chain:
  event dispatch → PopulateInstructionsObserver → instruction_writer → files in artifacts_dir
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from agento.framework.consumer import Consumer
from agento.modules.claude.src.output_parser import ClaudeResult

from .conftest import _test_connection, fetch_job, insert_primary_token


def _insert_workspace(code: str = "acme", label: str = "Acme Corp") -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace (code, label) VALUES (%s, %s)",
                (code, label),
            )
            return cur.lastrowid
    finally:
        conn.close()


def _insert_agent_view(workspace_id: int, code: str = "developer", label: str = "Developer") -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, %s, %s)",
                (workspace_id, code, label),
            )
            return cur.lastrowid
    finally:
        conn.close()


def _insert_scoped_config(scope: str, scope_id: int, path: str, value: str) -> None:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO core_config_data (scope, scope_id, path, value, encrypted)
                   VALUES (%s, %s, %s, %s, 0)
                   ON DUPLICATE KEY UPDATE value = VALUES(value)""",
                (scope, scope_id, path, value),
            )
    finally:
        conn.close()


def _insert_job_with_agent_view(agent_view_id: int, reference_id: str = "AI-1") -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO job (type, source, agent_view_id, reference_id,
                                    idempotency_key, status, attempt, max_attempts)
                   VALUES ('cron', 'jira', %s, %s, %s, 'TODO', 0, 3)""",
                (agent_view_id, reference_id, f"test:instr:{reference_id}"),
            )
            return cur.lastrowid
    finally:
        conn.close()


def _cleanup_test_data():
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute("DELETE FROM core_config_data WHERE path LIKE 'agent_view/instructions/%'")
            cur.execute("DELETE FROM job")
            cur.execute("DELETE FROM agent_view")
            cur.execute("DELETE FROM workspace")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    finally:
        conn.close()


class TestInstructionFilesFromScopedConfig:
    """Observer writes AGENTS.md/SOUL.md from core_config_data into run_dir."""

    @pytest.fixture(autouse=True)
    def _patch_observer_db(self, int_db_config):
        """Make the observer's DatabaseConfig.from_env() return the test DB config.
        Two observers need this: the agent_view PopulateInstructionsObserver and
        the workspace_build BuildFreshnessCheckObserver (the latter fires now on
        every job via workspace_build_check_before)."""
        with patch(
            "agento.modules.agent_view.src.observers.DatabaseConfig.from_env",
            return_value=int_db_config,
        ), patch(
            "agento.modules.workspace_build.src.observers.DatabaseConfig.from_env",
            return_value=int_db_config,
        ):
            yield

    def setup_method(self):
        _cleanup_test_data()

    def teardown_method(self):
        _cleanup_test_data()

    def test_writes_custom_agents_and_soul_md(self, int_db_config, int_consumer_config, tmp_path):
        insert_primary_token("claude")
        ws_id = _insert_workspace("acme")
        av_id = _insert_agent_view(ws_id, "developer")

        _insert_scoped_config("agent_view", av_id, "agent_view/instructions/agents_md", "# Custom AGENTS for developer")
        _insert_scoped_config("agent_view", av_id, "agent_view/instructions/soul_md", "# Custom SOUL for developer")

        job_id = _insert_job_with_agent_view(av_id)

        # Capture files from artifacts_dir DURING execution (before cleanup)
        captured_files = {}

        def capturing_run(self_runner, request):
            wd = Path(self_runner.context.working_dir)
            for name in ("AGENTS.md", "SOUL.md", "CLAUDE.md"):
                fpath = wd / name
                if fpath.exists():
                    captured_files[name] = fpath.read_text()
            return ClaudeResult(
                raw_output="ok", input_tokens=100, output_tokens=50,
                duration_ms=1000, session_id="success", harness="claude",
            )

        with patch("agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute", capturing_run), \
             patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path)), \
             patch("agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path)), \
             patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(tmp_path)):
            logger = logging.getLogger("test")
            consumer = Consumer(int_db_config, int_consumer_config, logger)
            job = consumer._try_dequeue()
            assert job is not None
            consumer._execute_job(job)

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS"

        assert "AGENTS.md" in captured_files
        assert captured_files["AGENTS.md"] == "# Custom AGENTS for developer"
        assert "SOUL.md" in captured_files
        assert captured_files["SOUL.md"] == "# Custom SOUL for developer"
        assert "CLAUDE.md" in captured_files

    def test_falls_back_to_workspace_files(self, int_db_config, int_consumer_config, tmp_path):
        """When no scoped config exists, copies from workspace directory."""
        insert_primary_token("claude")
        ws_id = _insert_workspace("acme")
        av_id = _insert_agent_view(ws_id, "qa-tester")

        # NO scoped config — should fall back to workspace files
        job_id = _insert_job_with_agent_view(av_id, reference_id="AI-2")

        # No scoped config AND workspace_dir points to tmp_path (no files there)
        # → AGENTS.md/SOUL.md should be absent, only CLAUDE.md written

        captured_files = {}

        def capturing_run(self_runner, request):
            wd = Path(self_runner.context.working_dir)
            for name in ("AGENTS.md", "SOUL.md", "CLAUDE.md"):
                fpath = wd / name
                if fpath.exists():
                    captured_files[name] = fpath.read_text()
            return ClaudeResult(
                raw_output="ok", input_tokens=100, output_tokens=50,
                duration_ms=1000, session_id="success", harness="claude",
            )

        with patch("agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute", capturing_run), \
             patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path)), \
             patch("agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path)), \
             patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(tmp_path)):
            logger = logging.getLogger("test")
            consumer = Consumer(int_db_config, int_consumer_config, logger)
            job = consumer._try_dequeue()
            assert job is not None
            consumer._execute_job(job)

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS"

        # CLAUDE.md always written
        assert "CLAUDE.md" in captured_files
        # AGENTS.md/SOUL.md not present (no config, no workspace file at test path)
        assert "AGENTS.md" not in captured_files
        assert "SOUL.md" not in captured_files

    def test_workspace_scope_fallback(self, int_db_config, int_consumer_config, tmp_path):
        """Config at workspace scope applies to all agent_views in that workspace."""
        insert_primary_token("claude")
        ws_id = _insert_workspace("acme")
        av_id = _insert_agent_view(ws_id, "team-lead")

        # Set config at WORKSPACE scope (not agent_view)
        _insert_scoped_config("workspace", ws_id, "agent_view/instructions/agents_md", "# Workspace-level AGENTS")

        job_id = _insert_job_with_agent_view(av_id, reference_id="AI-3")

        captured_files = {}

        def capturing_run(self_runner, request):
            wd = Path(self_runner.context.working_dir)
            for name in ("AGENTS.md", "SOUL.md", "CLAUDE.md"):
                fpath = wd / name
                if fpath.exists():
                    captured_files[name] = fpath.read_text()
            return ClaudeResult(
                raw_output="ok", input_tokens=100, output_tokens=50,
                duration_ms=1000, session_id="success", harness="claude",
            )

        with patch("agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute", capturing_run), \
             patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path)), \
             patch("agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path)), \
             patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(tmp_path)):
            logger = logging.getLogger("test")
            consumer = Consumer(int_db_config, int_consumer_config, logger)
            job = consumer._try_dequeue()
            assert job is not None
            consumer._execute_job(job)

        row = fetch_job(job_id)
        assert row is not None
        assert row["status"] == "SUCCESS"
        assert captured_files["AGENTS.md"] == "# Workspace-level AGENTS"
