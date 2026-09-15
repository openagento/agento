"""Tests for per-run instruction file writer."""
from agento.modules.agent_view.src.instruction_writer import (
    CLAUDE_MD_CONTENT,
    write_instruction_files,
)


class TestWriteFromConfig:
    def test_writes_agents_md_from_config(self, tmp_path):
        artifacts_dir = tmp_path / "run"
        artifacts_dir.mkdir()
        overrides = {"agent_view/instructions/agents_md": ("# Custom AGENTS", False)}

        write_instruction_files(artifacts_dir, overrides)

        assert (artifacts_dir / "AGENTS.md").read_text() == "# Custom AGENTS"

    def test_writes_soul_md_from_config(self, tmp_path):
        artifacts_dir = tmp_path / "run"
        artifacts_dir.mkdir()
        overrides = {"agent_view/instructions/soul_md": ("# Custom SOUL", False)}

        write_instruction_files(artifacts_dir, overrides)

        assert (artifacts_dir / "SOUL.md").read_text() == "# Custom SOUL"

    def test_db_value_overrides_workspace_file(self, tmp_path):
        artifacts_dir = tmp_path / "run"
        artifacts_dir.mkdir()
        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        (ws_dir / "AGENTS.md").write_text("workspace default")
        overrides = {"agent_view/instructions/agents_md": ("db override", False)}

        write_instruction_files(artifacts_dir, overrides, workspace_dir=ws_dir)

        assert (artifacts_dir / "AGENTS.md").read_text() == "db override"


class TestFallbackToWorkspace:
    def test_copies_from_workspace_when_no_config(self, tmp_path):
        artifacts_dir = tmp_path / "run"
        artifacts_dir.mkdir()
        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        (ws_dir / "AGENTS.md").write_text("workspace agents")
        (ws_dir / "SOUL.md").write_text("workspace soul")

        write_instruction_files(artifacts_dir, {}, workspace_dir=ws_dir)

        assert (artifacts_dir / "AGENTS.md").read_text() == "workspace agents"
        assert (artifacts_dir / "SOUL.md").read_text() == "workspace soul"

    def test_no_workspace_file_no_config_skips(self, tmp_path):
        artifacts_dir = tmp_path / "run"
        artifacts_dir.mkdir()
        ws_dir = tmp_path / "empty_workspace"
        ws_dir.mkdir()

        write_instruction_files(artifacts_dir, {}, workspace_dir=ws_dir)

        assert not (artifacts_dir / "AGENTS.md").exists()
        assert not (artifacts_dir / "SOUL.md").exists()


class TestClaudeMd:
    def test_writes_claude_md_when_an_agents_md_was_written(self, tmp_path):
        artifacts_dir = tmp_path / "run"
        artifacts_dir.mkdir()
        overrides = {"agent_view/instructions/agents_md": ("# Custom AGENTS", False)}

        write_instruction_files(artifacts_dir, overrides)

        assert (artifacts_dir / "CLAUDE.md").read_text() == CLAUDE_MD_CONTENT

    def test_writes_no_claude_md_without_an_agents_md(self, tmp_path):
        """The regression: CLAUDE.md only says "read AGENTS.md", so writing it with no
        AGENTS.md gave the agent a dead end as its ONLY entry point."""
        artifacts_dir = tmp_path / "run"
        artifacts_dir.mkdir()
        ws_dir = tmp_path / "empty"
        ws_dir.mkdir()

        write_instruction_files(artifacts_dir, {}, workspace_dir=ws_dir)

        assert not (artifacts_dir / "CLAUDE.md").exists()
        assert not (artifacts_dir / "AGENTS.md").exists()

    def test_removes_a_claude_md_copied_from_a_build_with_no_agents_md(self, tmp_path):
        """The run dir is a COPY of the build, so a stale pointer arrives by copy."""
        artifacts_dir = tmp_path / "run"
        artifacts_dir.mkdir()
        (artifacts_dir / "CLAUDE.md").write_text(CLAUDE_MD_CONTENT)
        ws_dir = tmp_path / "empty"
        ws_dir.mkdir()

        write_instruction_files(artifacts_dir, {}, workspace_dir=ws_dir)

        assert not (artifacts_dir / "CLAUDE.md").exists()


class TestEmptyConfigValue:
    def test_empty_string_falls_back_to_workspace(self, tmp_path):
        artifacts_dir = tmp_path / "run"
        artifacts_dir.mkdir()
        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        (ws_dir / "AGENTS.md").write_text("workspace default")
        overrides = {"agent_view/instructions/agents_md": ("", False)}

        write_instruction_files(artifacts_dir, overrides, workspace_dir=ws_dir)

        assert (artifacts_dir / "AGENTS.md").read_text() == "workspace default"
