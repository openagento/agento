"""Integration tests: composable_skills — skill registry sync + scoped enable/disable.

Uses real MySQL. Tests disk scan → DB sync → scoped enable/disable → query enabled skills.
"""
from __future__ import annotations

from .conftest import _test_connection


def _cleanup():
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute("DELETE FROM agent_view")
            cur.execute("DELETE FROM workspace")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    finally:
        conn.close()


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


def _count_skills() -> int:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM skill_registry")
            return cur.fetchone()["cnt"]
    finally:
        conn.close()


def _get_skill(name: str) -> dict | None:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM skill_registry WHERE name = %s", (name,))
            return cur.fetchone()
    finally:
        conn.close()


class TestSkillSyncIntegration:
    """scan_skills + sync_skills with real DB."""

    def setup_method(self):
        _cleanup()

    def teardown_method(self):
        _cleanup()

    def test_sync_creates_new_skills(self, tmp_path):
        from agento.modules.skill.src.registry import sync_skills

        (tmp_path / "git-workflow").mkdir()
        (tmp_path / "git-workflow" / "SKILL.md").write_text("# Git Workflow\nManage branches and PRs.")

        (tmp_path / "code-review").mkdir()
        (tmp_path / "code-review" / "SKILL.md").write_text("# Code Review\nReview code for quality.")

        conn = _test_connection(autocommit=False)
        try:
            result = sync_skills(conn, tmp_path)
        finally:
            conn.close()

        assert result.new == 2
        assert result.updated == 0
        assert result.unchanged == 0
        assert _count_skills() == 2

        skill = _get_skill("git-workflow")
        assert skill is not None
        assert skill["description"] == "Manage branches and PRs."
        assert len(skill["checksum"]) == 64

    def test_sync_updates_changed_skills(self, tmp_path):
        from agento.modules.skill.src.registry import sync_skills

        (tmp_path / "my-skill").mkdir()
        (tmp_path / "my-skill" / "SKILL.md").write_text("# V1\nOriginal content.")

        conn = _test_connection(autocommit=False)
        try:
            sync_skills(conn, tmp_path)
        finally:
            conn.close()

        old_checksum = _get_skill("my-skill")["checksum"]

        # Change content
        (tmp_path / "my-skill" / "SKILL.md").write_text("# V2\nUpdated content.")

        conn = _test_connection(autocommit=False)
        try:
            result = sync_skills(conn, tmp_path)
        finally:
            conn.close()

        assert result.new == 0
        assert result.updated == 1
        assert _get_skill("my-skill")["checksum"] != old_checksum

    def test_sync_unchanged_is_idempotent(self, tmp_path):
        from agento.modules.skill.src.registry import sync_skills

        (tmp_path / "stable").mkdir()
        (tmp_path / "stable" / "SKILL.md").write_text("# Stable\nNo changes.")

        conn = _test_connection(autocommit=False)
        try:
            sync_skills(conn, tmp_path)
        finally:
            conn.close()

        conn = _test_connection(autocommit=False)
        try:
            result = sync_skills(conn, tmp_path)
        finally:
            conn.close()

        assert result.unchanged == 1
        assert result.new == 0
        assert result.updated == 0

    def test_sync_ignores_dirs_without_skill_md(self, tmp_path):
        from agento.modules.skill.src.registry import sync_skills

        (tmp_path / "no-skill-file").mkdir()
        (tmp_path / "no-skill-file" / "README.md").write_text("Not a skill.")

        (tmp_path / "valid").mkdir()
        (tmp_path / "valid" / "SKILL.md").write_text("# Valid\nThis is a skill.")

        conn = _test_connection(autocommit=False)
        try:
            result = sync_skills(conn, tmp_path)
        finally:
            conn.close()

        assert result.new == 1
        assert _count_skills() == 1

    def test_sync_ignores_hidden_and_underscore_dirs(self, tmp_path):
        from agento.modules.skill.src.registry import sync_skills

        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "SKILL.md").write_text("# Hidden")

        (tmp_path / "_internal").mkdir()
        (tmp_path / "_internal" / "SKILL.md").write_text("# Internal")

        conn = _test_connection(autocommit=False)
        try:
            result = sync_skills(conn, tmp_path)
        finally:
            conn.close()

        assert result.new == 0


class TestSkillScopedEnableDisable:
    """Enable/disable skills per agent_view via scoped config."""

    def setup_method(self):
        _cleanup()

    def teardown_method(self):
        _cleanup()

    def _setup_skills(self, tmp_path) -> tuple[int, int]:
        """Create 2 skills in DB, workspace + agent_view. Returns (ws_id, av_id)."""
        from agento.modules.skill.src.registry import sync_skills

        for name in ("skill-a", "skill-b"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "SKILL.md").write_text(f"# {name}\nDescription of {name}.")

        conn = _test_connection(autocommit=False)
        try:
            sync_skills(conn, tmp_path)
        finally:
            conn.close()

        ws_id = _insert_workspace()
        av_id = _insert_agent_view(ws_id)
        return ws_id, av_id

    def test_no_skills_enabled_by_default(self, tmp_path):
        from agento.modules.skill.src.registry import get_enabled_skills

        ws_id, av_id = self._setup_skills(tmp_path)

        conn = _test_connection(autocommit=True)
        try:
            enabled = get_enabled_skills(conn, agent_view_id=av_id, workspace_id=ws_id)
        finally:
            conn.close()

        # Opt-in: nothing is enabled until an explicit '1' row exists.
        assert len(enabled) == 0

    def test_enable_skill_for_agent_view(self, tmp_path):
        from agento.framework.scoped_config import scoped_config_set
        from agento.modules.skill.src.registry import get_enabled_skills

        ws_id, av_id = self._setup_skills(tmp_path)

        conn = _test_connection(autocommit=True)
        try:
            scoped_config_set(conn, "skill/skill-a/is_enabled", "1", scope="agent_view", scope_id=av_id)
            enabled = get_enabled_skills(conn, agent_view_id=av_id, workspace_id=ws_id)
        finally:
            conn.close()

        assert len(enabled) == 1
        assert enabled[0].name == "skill-a"

    def test_enable_skill_globally_inherited_by_agent_view(self, tmp_path):
        from agento.framework.scoped_config import scoped_config_set
        from agento.modules.skill.src.registry import get_enabled_skills

        ws_id, av_id = self._setup_skills(tmp_path)

        conn = _test_connection(autocommit=True)
        try:
            scoped_config_set(conn, "skill/skill-a/is_enabled", "1", scope="default", scope_id=0)
            enabled = get_enabled_skills(conn, agent_view_id=av_id, workspace_id=ws_id)
        finally:
            conn.close()

        # agent_view has no own row -> inherits the default enable.
        assert len(enabled) == 1
        assert enabled[0].name == "skill-a"

    def test_agent_view_disable_overrides_default_enable(self, tmp_path):
        from agento.framework.scoped_config import scoped_config_set
        from agento.modules.skill.src.registry import get_enabled_skills

        ws_id, av_id = self._setup_skills(tmp_path)

        conn = _test_connection(autocommit=True)
        try:
            scoped_config_set(conn, "skill/skill-a/is_enabled", "1", scope="default", scope_id=0)
            scoped_config_set(conn, "skill/skill-b/is_enabled", "1", scope="default", scope_id=0)
            scoped_config_set(conn, "skill/skill-a/is_enabled", "0", scope="agent_view", scope_id=av_id)
            enabled = get_enabled_skills(conn, agent_view_id=av_id, workspace_id=ws_id)
        finally:
            conn.close()

        assert len(enabled) == 1
        assert enabled[0].name == "skill-b"

    def test_agent_view_re_enables_globally_disabled_skill(self, tmp_path):
        from agento.framework.scoped_config import scoped_config_set
        from agento.modules.skill.src.registry import get_enabled_skills

        ws_id, av_id = self._setup_skills(tmp_path)

        conn = _test_connection(autocommit=True)
        try:
            scoped_config_set(conn, "skill/skill-a/is_enabled", "0", scope="default", scope_id=0)
            scoped_config_set(conn, "skill/skill-a/is_enabled", "1", scope="agent_view", scope_id=av_id)
            enabled = get_enabled_skills(conn, agent_view_id=av_id, workspace_id=ws_id)
        finally:
            conn.close()

        names = {s.name for s in enabled}
        assert "skill-a" in names

    def test_get_skill_content_from_disk(self, tmp_path):
        from agento.modules.skill.src.registry import get_skill_content

        (tmp_path / "my-skill").mkdir()
        (tmp_path / "my-skill" / "SKILL.md").write_text("# My Skill\nDo useful things.")

        content = get_skill_content("my-skill", tmp_path)
        assert content is not None
        assert "Do useful things" in content

    def test_get_skill_content_returns_none_for_missing(self, tmp_path):
        from agento.modules.skill.src.registry import get_skill_content

        assert get_skill_content("nonexistent", tmp_path) is None
