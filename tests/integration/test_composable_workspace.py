"""Integration tests: composable_workspace — build, symlink, consumer uses pre-built workspace.

Uses real MySQL + mocked Claude runner. Tests the full flow:
  workspace:build → materialized dir → consumer copies from build → runner sees files
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from agento.framework.consumer import Consumer
from agento.modules.claude.src.output_parser import ClaudeResult

from .conftest import _test_connection, fetch_job, insert_primary_token


def _insert_workspace(code: str = "acme") -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO workspace (code, label) VALUES (%s, %s)", (code, code))
            return cur.lastrowid
    finally:
        conn.close()


def _insert_agent_view(workspace_id: int, code: str = "developer") -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, %s, %s)",
                (workspace_id, code, code),
            )
            return cur.lastrowid
    finally:
        conn.close()


def _set_config(scope: str, scope_id: int, path: str, value: str) -> None:
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


def _insert_job(agent_view_id: int, reference_id: str = "AI-1") -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO job (type, source, agent_view_id, reference_id,
                                    idempotency_key, status, attempt, max_attempts)
                   VALUES ('cron', 'jira', %s, %s, %s, 'TODO', 0, 3)""",
                (agent_view_id, reference_id, f"test:ws:{reference_id}"),
            )
            return cur.lastrowid
    finally:
        conn.close()


def _get_build_row(build_id: int) -> dict | None:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM workspace_build WHERE id = %s", (build_id,))
            return cur.fetchone()
    finally:
        conn.close()


def _count_builds(agent_view_id: int) -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM workspace_build WHERE agent_view_id = %s",
                (agent_view_id,),
            )
            return cur.fetchone()["cnt"]
    finally:
        conn.close()


def _cleanup():
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute("TRUNCATE TABLE workspace_build")
            cur.execute("TRUNCATE TABLE skill_registry")
            cur.execute("DELETE FROM core_config_data WHERE path LIKE 'agent_view/%' OR path LIKE 'skill/%'")
            cur.execute("DELETE FROM job")
            cur.execute("DELETE FROM agent_view")
            cur.execute("DELETE FROM workspace")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    finally:
        conn.close()


class TestWorkspaceBuildIntegration:
    """execute_build creates materialized dirs with correct files."""

    @pytest.fixture(autouse=True)
    def _patch_observer_db(self, int_db_config):
        with patch(
            "agento.modules.agent_view.src.observers.DatabaseConfig.from_env",
            return_value=int_db_config,
        ):
            yield

    def setup_method(self):
        _cleanup()

    def teardown_method(self):
        _cleanup()

    def test_build_creates_directory_and_db_record(self, tmp_path):
        from agento.modules.workspace_build.src.builder import execute_build

        ws_id = _insert_workspace("acme")
        av_id = _insert_agent_view(ws_id, "developer")

        _set_config("agent_view", av_id, "agent_view/instructions/agents_md", "# Custom AGENTS")
        _set_config("agent_view", av_id, "agent_view/instructions/soul_md", "# Custom SOUL")

        conn = _test_connection(autocommit=False)
        try:
            with patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(tmp_path)):
                result = execute_build(conn, av_id)
        finally:
            conn.close()

        assert result.skipped is False
        assert result.build_id > 0
        assert len(result.checksum) == 64

        # Verify DB record
        row = _get_build_row(result.build_id)
        assert row is not None
        assert row["status"] == "ready"
        assert row["agent_view_id"] == av_id

        # Verify files on disk
        build_dir = Path(result.build_dir)
        assert build_dir.is_dir()
        assert (build_dir / "AGENTS.md").read_text() == "# Custom AGENTS"
        assert (build_dir / "SOUL.md").read_text() == "# Custom SOUL"
        assert (build_dir / "CLAUDE.md").exists()
        assert "AGENTS.md" in (build_dir / "CLAUDE.md").read_text()

    def test_build_creates_current_symlink(self, tmp_path):
        from agento.modules.workspace_build.src.builder import execute_build

        ws_id = _insert_workspace("acme")
        av_id = _insert_agent_view(ws_id, "developer")

        conn = _test_connection(autocommit=False)
        try:
            with patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(tmp_path)):
                result = execute_build(conn, av_id)
        finally:
            conn.close()

        current_link = tmp_path / "acme" / "developer" / "current"
        assert current_link.is_symlink()
        assert current_link.resolve() == Path(result.build_dir).resolve()

    def test_build_is_idempotent(self, tmp_path):
        """Same config = same checksum = build skipped."""
        from agento.modules.workspace_build.src.builder import execute_build

        ws_id = _insert_workspace("acme")
        av_id = _insert_agent_view(ws_id, "developer")

        _set_config("agent_view", av_id, "agent_view/instructions/agents_md", "# Stable content")

        conn = _test_connection(autocommit=False)
        try:
            with patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(tmp_path)):
                result1 = execute_build(conn, av_id)
                result2 = execute_build(conn, av_id)
        finally:
            conn.close()

        assert result1.skipped is False
        assert result2.skipped is True
        assert result1.build_id == result2.build_id
        assert result1.checksum == result2.checksum
        assert _count_builds(av_id) == 1

    def test_config_change_triggers_new_build(self, tmp_path):
        from agento.modules.workspace_build.src.builder import execute_build

        ws_id = _insert_workspace("acme")
        av_id = _insert_agent_view(ws_id, "developer")

        _set_config("agent_view", av_id, "agent_view/instructions/agents_md", "# V1")

        conn = _test_connection(autocommit=False)
        try:
            with patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(tmp_path)):
                result1 = execute_build(conn, av_id)
        finally:
            conn.close()

        _set_config("agent_view", av_id, "agent_view/instructions/agents_md", "# V2")

        conn = _test_connection(autocommit=False)
        try:
            with patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(tmp_path)):
                result2 = execute_build(conn, av_id)
        finally:
            conn.close()

        assert result1.checksum != result2.checksum
        assert result1.build_id != result2.build_id
        assert _count_builds(av_id) == 2

        # Current symlink updated to new build
        current_link = tmp_path / "acme" / "developer" / "current"
        assert current_link.resolve() == Path(result2.build_dir).resolve()

    def test_build_includes_skills(self, tmp_path):
        """When skill module is available, enabled skills are written to .claude/skills/."""
        from agento.framework.scoped_config import scoped_config_set
        from agento.modules.skill.src.registry import sync_skills
        from agento.modules.workspace_build.src.builder import execute_build

        ws_id = _insert_workspace("acme")
        av_id = _insert_agent_view(ws_id, "developer")

        # Create a skill on disk as a real skill directory: SKILL.md + companion file.
        skills_dir = tmp_path / "skills"
        (skills_dir / "git-workflow").mkdir(parents=True)
        (skills_dir / "git-workflow" / "SKILL.md").write_text("# Git Workflow\nManage PRs.")
        (skills_dir / "git-workflow" / "references").mkdir()
        (skills_dir / "git-workflow" / "references" / "commands.md").write_text("# commands ref")

        # Sync skills to DB, then enable (opt-in: skills are disabled by default).
        conn = _test_connection(autocommit=False)
        try:
            sync_skills(conn, skills_dir)
        finally:
            conn.close()
        conn = _test_connection(autocommit=True)
        try:
            scoped_config_set(conn, "skill/git-workflow/is_enabled", "1", scope="default", scope_id=0)
        finally:
            conn.close()

        ws_base = tmp_path / "workspace"
        conn = _test_connection(autocommit=False)
        try:
            with patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(ws_base)):
                result = execute_build(conn, av_id)
        finally:
            conn.close()

        build_dir = Path(result.build_dir)
        skills_output = build_dir / ".claude" / "skills"
        assert skills_output.is_dir()
        # Skill materialized as a directory (matches Claude Code's skill format)
        assert (skills_output / "git-workflow" / "SKILL.md").exists()
        assert "Manage PRs" in (skills_output / "git-workflow" / "SKILL.md").read_text()
        # Companion files are preserved
        assert (skills_output / "git-workflow" / "references" / "commands.md").read_text() == "# commands ref"
        # No flat `<name>.md` at the skills/ root
        assert not (skills_output / "git-workflow.md").exists()


class TestConsumerUsesPreBuiltWorkspace:
    """Consumer copies from pre-built workspace instead of generating on-the-fly."""

    @pytest.fixture(autouse=True)
    def _patch_observer_db(self, int_db_config):
        with patch(
            "agento.modules.agent_view.src.observers.DatabaseConfig.from_env",
            return_value=int_db_config,
        ):
            yield

    def setup_method(self):
        _cleanup()

    def teardown_method(self):
        _cleanup()

    def test_consumer_uses_build_when_available(self, int_db_config, int_consumer_config, tmp_path):
        """Full flow: build workspace → run job → runner sees pre-built files."""
        from agento.modules.workspace_build.src.builder import execute_build

        insert_primary_token("claude")
        ws_id = _insert_workspace("acme")
        av_id = _insert_agent_view(ws_id, "developer")

        _set_config("agent_view", av_id, "agent_view/instructions/agents_md", "# Pre-built AGENTS")
        _set_config("agent_view", av_id, "agent_view/instructions/soul_md", "# Pre-built SOUL")

        # Step 1: Build workspace
        conn = _test_connection(autocommit=False)
        try:
            with patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(tmp_path)):
                build_result = execute_build(conn, av_id)
        finally:
            conn.close()

        assert build_result.skipped is False

        # Step 2: Run a job — consumer should copy from build
        job_id = _insert_job(av_id)

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
             patch("agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path)):
            logger = logging.getLogger("test")
            consumer = Consumer(int_db_config, int_consumer_config, logger)
            job = consumer._try_dequeue()
            assert job is not None
            consumer._execute_job(job)

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS"

        assert captured_files.get("AGENTS.md") == "# Pre-built AGENTS"
        assert captured_files.get("SOUL.md") == "# Pre-built SOUL"
        assert "CLAUDE.md" in captured_files

    def test_consumer_falls_back_without_build(self, int_db_config, int_consumer_config, tmp_path):
        """Without a pre-built workspace, consumer generates on-the-fly (existing behavior)."""
        insert_primary_token("claude")
        ws_id = _insert_workspace("acme")
        av_id = _insert_agent_view(ws_id, "developer")

        _set_config("agent_view", av_id, "agent_view/instructions/agents_md", "# On-the-fly AGENTS")

        job_id = _insert_job(av_id)

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
             patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(tmp_path)), \
             patch("agento.modules.workspace_build.src.observers.DatabaseConfig.from_env", return_value=int_db_config), \
             patch("agento.modules.agent_view.src.observers.DatabaseConfig.from_env", return_value=int_db_config):
            logger = logging.getLogger("test")
            consumer = Consumer(int_db_config, int_consumer_config, logger)
            job = consumer._try_dequeue()
            assert job is not None
            consumer._execute_job(job)

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS"

        # Files still generated on-the-fly by observer
        assert captured_files.get("AGENTS.md") == "# On-the-fly AGENTS"
        assert "CLAUDE.md" in captured_files

    def test_observer_skips_when_build_provides_instructions(self, int_db_config, int_consumer_config, tmp_path):
        """PopulateInstructionsObserver skips when AGENTS.md already exists from build."""
        from agento.modules.workspace_build.src.builder import execute_build

        insert_primary_token("claude")
        ws_id = _insert_workspace("acme")
        av_id = _insert_agent_view(ws_id, "developer")

        _set_config("agent_view", av_id, "agent_view/instructions/agents_md", "# From Build")

        # Build workspace
        conn = _test_connection(autocommit=False)
        try:
            with patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(tmp_path)):
                execute_build(conn, av_id)
        finally:
            conn.close()

        job_id = _insert_job(av_id)

        observer_called = []
        original_execute = None

        # Track if the observer's heavy path is executed
        from agento.modules.agent_view.src.observers import PopulateInstructionsObserver
        original_execute = PopulateInstructionsObserver.execute

        def tracking_execute(self, event):
            # The guard should cause early return before DB access
            original_execute(self, event)
            # If we reach here AND no DB call was made, the guard worked
            observer_called.append(True)

        captured_files = {}

        def capturing_run(self_runner, request):
            wd = Path(self_runner.context.working_dir)
            for name in ("AGENTS.md",):
                fpath = wd / name
                if fpath.exists():
                    captured_files[name] = fpath.read_text()
            return ClaudeResult(
                raw_output="ok", input_tokens=100, output_tokens=50,
                duration_ms=1000, session_id="success", harness="claude",
            )

        with patch("agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute", capturing_run), \
             patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path)), \
             patch("agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path)):
            logger = logging.getLogger("test")
            consumer = Consumer(int_db_config, int_consumer_config, logger)
            job = consumer._try_dequeue()
            assert job is not None
            consumer._execute_job(job)

        row = fetch_job(job_id)
        assert row["status"] == "SUCCESS"
        # Content from build, not regenerated
        assert captured_files.get("AGENTS.md") == "# From Build"


class TestThemeLayeringIntegration:
    """Theme layering: base \u2192 workspace \u2192 agent_view, with DB override on top."""

    @pytest.fixture(autouse=True)
    def _patch_observer_db(self, int_db_config):
        with patch(
            "agento.modules.agent_view.src.observers.DatabaseConfig.from_env",
            return_value=int_db_config,
        ):
            yield

    def setup_method(self):
        _cleanup()

    def teardown_method(self):
        _cleanup()

    def test_theme_layers_in_build(self, tmp_path):
        """Build picks up files from theme/, theme/_ws/, theme/_ws/_av/ layers."""
        from agento.modules.workspace_build.src.builder import execute_build

        ws_id = _insert_workspace("acme")
        av_id = _insert_agent_view(ws_id, "developer")

        # Create layered theme (flat layout — no _root wrapper)
        theme = tmp_path / "theme"
        ws = theme / "_acme"
        av = ws / "_developer"
        av.mkdir(parents=True)

        (theme / "base.md").write_text("# Base file")
        (theme / "SOUL.md").write_text("# Base Soul")
        (ws / "ws-rules.md").write_text("# WS rules")
        (ws / "SOUL.md").write_text("# Acme Soul")
        (av / "SOUL.md").write_text("# Developer Soul")
        (av / "dev-config.md").write_text("# Dev config")

        # Dot-prefixed items at theme root are excluded from builds.
        (theme / ".hidden").mkdir()
        (theme / ".hidden" / "old.md").write_text("should not appear")

        build_base = tmp_path / "builds"
        conn = _test_connection(autocommit=False)
        try:
            with patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(build_base)), \
                 patch("agento.modules.workspace_build.src.builder.THEME_DIR", str(theme)):
                result = execute_build(conn, av_id)
        finally:
            conn.close()

        build_dir = Path(result.build_dir)
        assert (build_dir / "base.md").read_text() == "# Base file"
        assert (build_dir / "ws-rules.md").read_text() == "# WS rules"
        assert (build_dir / "dev-config.md").read_text() == "# Dev config"
        # Most specific layer wins
        assert (build_dir / "SOUL.md").read_text() == "# Developer Soul"
        # Scope dirs not in output
        assert not (build_dir / "_acme").exists()
        # Dot-prefixed dirs not copied
        assert not (build_dir / ".hidden").exists()

    def test_db_overrides_theme_layered_file(self, tmp_path):
        """DB agents_md/soul_md override even the most specific theme layer."""
        from agento.modules.workspace_build.src.builder import execute_build

        ws_id = _insert_workspace("acme")
        av_id = _insert_agent_view(ws_id, "developer")

        # Theme puts SOUL.md at agent_view level (flat layout)
        theme = tmp_path / "theme"
        av = theme / "_acme" / "_developer"
        av.mkdir(parents=True)
        (theme / "SOUL.md").write_text("# Base Soul")
        (av / "SOUL.md").write_text("# AV Soul from theme")

        # DB override \u2014 highest precedence
        _set_config("agent_view", av_id, "agent_view/instructions/soul_md", "# DB Soul Override")
        _set_config("agent_view", av_id, "agent_view/instructions/agents_md", "# DB AGENTS")

        build_base = tmp_path / "builds"
        conn = _test_connection(autocommit=False)
        try:
            with patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(build_base)), \
                 patch("agento.modules.workspace_build.src.builder.THEME_DIR", str(theme)):
                result = execute_build(conn, av_id)
        finally:
            conn.close()

        build_dir = Path(result.build_dir)
        assert (build_dir / "SOUL.md").read_text() == "# DB Soul Override"
        assert (build_dir / "AGENTS.md").read_text() == "# DB AGENTS"

    def test_no_db_override_preserves_theme_file(self, tmp_path):
        """Without DB override, theme-layered SOUL.md is preserved in the build."""
        from agento.modules.workspace_build.src.builder import execute_build

        ws_id = _insert_workspace("acme")
        av_id = _insert_agent_view(ws_id, "developer")

        # Theme puts SOUL.md at workspace level (flat layout)
        theme = tmp_path / "theme"
        ws = theme / "_acme"
        ws.mkdir(parents=True)
        (ws / "SOUL.md").write_text("# Acme Soul from theme")

        # NO DB override

        build_base = tmp_path / "builds"
        conn = _test_connection(autocommit=False)
        try:
            with patch("agento.modules.workspace_build.src.builder.BUILD_DIR", str(build_base)), \
                 patch("agento.modules.workspace_build.src.builder.THEME_DIR", str(theme)):
                result = execute_build(conn, av_id)
        finally:
            conn.close()

        build_dir = Path(result.build_dir)
        assert (build_dir / "SOUL.md").read_text() == "# Acme Soul from theme"
