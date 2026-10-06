"""Tests for skill registry — scan, sync, enabled filtering."""
from unittest.mock import MagicMock, patch

from agento.modules.skill.src.registry import (
    SkillInfo,
    SyncResult,
    get_all_skills,
    get_enabled_skills,
    get_skill_content,
    scan_skills,
    scan_skills_multi,
    sync_skills,
)

# -- scan_skills tests (filesystem, uses tmp_path) --


class TestScanSkills:
    def test_empty_dir(self, tmp_path):
        assert scan_skills(tmp_path) == []

    def test_nonexistent_dir(self, tmp_path):
        assert scan_skills(tmp_path / "nope") == []

    def test_ignores_hidden_and_underscore(self, tmp_path):
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "SKILL.md").write_text("hidden")
        (tmp_path / "_draft").mkdir()
        (tmp_path / "_draft" / "SKILL.md").write_text("draft")
        assert scan_skills(tmp_path) == []

    def test_ignores_dir_without_skill_md(self, tmp_path):
        (tmp_path / "no_skill").mkdir()
        (tmp_path / "no_skill" / "README.md").write_text("not a skill")
        assert scan_skills(tmp_path) == []

    def test_scans_valid_skill(self, tmp_path):
        skill_dir = tmp_path / "my_skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# My Skill\nDoes something useful.")

        result = scan_skills(tmp_path)
        assert len(result) == 1
        assert result[0].name == "my_skill"
        assert result[0].path == str(skill_dir / "SKILL.md")
        assert result[0].description == "Does something useful."
        assert len(result[0].checksum) == 64

    def test_description_skips_heading(self, tmp_path):
        skill_dir = tmp_path / "alpha"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Heading\n\nActual description here.")
        result = scan_skills(tmp_path)
        assert result[0].description == "Actual description here."

    def test_sorted_order(self, tmp_path):
        for name in ["zeta", "alpha", "mid"]:
            d = tmp_path / name
            d.mkdir()
            (d / "SKILL.md").write_text(f"# {name}\nDesc for {name}.")
        result = scan_skills(tmp_path)
        assert [s.name for s in result] == ["alpha", "mid", "zeta"]

    def test_checksum_changes_with_content(self, tmp_path):
        skill_dir = tmp_path / "check"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("version 1")
        r1 = scan_skills(tmp_path)

        (skill_dir / "SKILL.md").write_text("version 2")
        r2 = scan_skills(tmp_path)

        assert r1[0].checksum != r2[0].checksum

    def test_description_from_frontmatter_inline(self, tmp_path):
        skill_dir = tmp_path / "fm"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: fm\ndescription: Use when testing frontmatter.\n---\n# Fm\nBody line.")
        assert scan_skills(tmp_path)[0].description == "Use when testing frontmatter."

    def test_description_from_frontmatter_folded(self, tmp_path):
        skill_dir = tmp_path / "folded"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: folded\ndescription: >\n  Use when one\n  spans two lines.\nuser-invocable: true\n---\n# F\n")
        assert scan_skills(tmp_path)[0].description == "Use when one spans two lines."

    def test_frontmatter_without_description_uses_first_body_line(self, tmp_path):
        skill_dir = tmp_path / "nodesc"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: nodesc\n---\n# Nodesc\n\nFirst body line.")
        assert scan_skills(tmp_path)[0].description == "First body line."


# -- DB-backed tests (mocked connection) --


_SENTINEL = object()


def _mock_conn(rows=None, fetchone=_SENTINEL):
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    if fetchone is not _SENTINEL:
        cursor.fetchone.return_value = fetchone
    if rows is not None:
        cursor.fetchall.return_value = rows
    return conn, cursor


class TestSyncSkills:
    def test_inserts_new_skill(self, tmp_path):
        skill_dir = tmp_path / "new_skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# New\nBrand new skill.")

        conn, _cursor = _mock_conn(fetchone=None)
        result = sync_skills(conn, tmp_path)

        assert result.new == 1
        assert result.updated == 0
        assert result.unchanged == 0
        conn.commit.assert_called_once()

    def test_updates_changed_skill(self, tmp_path):
        skill_dir = tmp_path / "existing"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Existing\nUpdated content.")

        conn, _cursor = _mock_conn(fetchone={"id": 1, "checksum": "old_checksum"})
        result = sync_skills(conn, tmp_path)

        assert result.new == 0
        assert result.updated == 1
        assert result.unchanged == 0

    def test_unchanged_skill(self, tmp_path):
        skill_dir = tmp_path / "stable"
        skill_dir.mkdir()
        content = "# Stable\nSame content."
        (skill_dir / "SKILL.md").write_text(content)

        import hashlib
        checksum = hashlib.sha256(content.encode()).hexdigest()
        conn, _cursor = _mock_conn(fetchone={"id": 1, "checksum": checksum, "description": "Same content."})
        result = sync_skills(conn, tmp_path)

        assert result.new == 0
        assert result.updated == 0
        assert result.unchanged == 1

    def test_same_checksum_stale_description_is_updated(self, tmp_path):
        # A row stored before the frontmatter parse kept "---" as its description.
        skill_dir = tmp_path / "fm"
        skill_dir.mkdir()
        content = "---\nname: fm\ndescription: Real description.\n---\n# Fm\n"
        (skill_dir / "SKILL.md").write_text(content)

        import hashlib
        checksum = hashlib.sha256(content.encode()).hexdigest()
        conn, cursor = _mock_conn(fetchone={"id": 1, "checksum": checksum, "description": "---"})
        result = sync_skills(conn, tmp_path)

        assert result.updated == 1
        update_sql, params = cursor.execute.call_args_list[-1].args
        assert update_sql.startswith("UPDATE skill_registry")
        assert params[1] == "Real description."

    def test_empty_dir_no_writes(self, tmp_path):
        conn, _cursor = _mock_conn()
        result = sync_skills(conn, tmp_path)
        assert result == SyncResult(new=0, updated=0, unchanged=0)
        conn.commit.assert_called_once()


class TestGetAllSkills:
    def test_returns_skills_from_db(self):
        rows = [
            {"name": "alpha", "path": "/a/SKILL.md", "description": "Alpha desc", "checksum": "aaa"},
            {"name": "beta", "path": "/b/SKILL.md", "description": "Beta desc", "checksum": "bbb"},
        ]
        conn, _ = _mock_conn(rows=rows)
        result = get_all_skills(conn)
        assert len(result) == 2
        assert result[0].name == "alpha"
        assert result[1].name == "beta"

    def test_returns_empty_list(self):
        conn, _ = _mock_conn(rows=[])
        assert get_all_skills(conn) == []


class TestGetEnabledSkills:
    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.modules.skill.src.registry.get_all_skills")
    def test_none_enabled_by_default(self, mock_get_all, mock_overrides):
        # Opt-in: with no override rows, every skill is disabled.
        mock_get_all.return_value = [
            SkillInfo(name="a", path="/a", description="", checksum=""),
            SkillInfo(name="b", path="/b", description="", checksum=""),
        ]
        mock_overrides.return_value = {}
        conn = MagicMock()

        result = get_enabled_skills(conn)
        assert len(result) == 0

    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.modules.skill.src.registry.get_all_skills")
    def test_only_explicitly_enabled_included(self, mock_get_all, mock_overrides):
        mock_get_all.return_value = [
            SkillInfo(name="a", path="/a", description="", checksum=""),
            SkillInfo(name="b", path="/b", description="", checksum=""),
        ]
        # "a" enabled with '1'; "b" explicitly disabled with '0'.
        mock_overrides.return_value = {
            "skill/a/is_enabled": ("1", False),
            "skill/b/is_enabled": ("0", False),
        }
        conn = MagicMock()

        result = get_enabled_skills(conn)
        assert len(result) == 1
        assert result[0].name == "a"

    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.modules.skill.src.registry.get_all_skills")
    def test_explicitly_enabled_included(self, mock_get_all, mock_overrides):
        mock_get_all.return_value = [
            SkillInfo(name="a", path="/a", description="", checksum=""),
        ]
        mock_overrides.return_value = {"skill/a/is_enabled": ("1", False)}
        conn = MagicMock()

        result = get_enabled_skills(conn)
        assert len(result) == 1

    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.modules.skill.src.registry.get_all_skills")
    def test_three_state_semantics(self, mock_get_all, mock_overrides):
        # build_scoped_overrides already merges agent_view > workspace > default
        # into one value per path, so we assert against the merged result:
        #   missing -> disabled, '1' -> enabled, '0' -> disabled.
        mock_get_all.return_value = [
            SkillInfo(name="missing", path="/m", description="", checksum=""),
            SkillInfo(name="on", path="/o", description="", checksum=""),
            SkillInfo(name="off", path="/f", description="", checksum=""),
        ]
        mock_overrides.return_value = {
            "skill/on/is_enabled": ("1", False),
            "skill/off/is_enabled": ("0", False),  # e.g. agent_view '0' over inherited '1'
        }
        conn = MagicMock()

        result = get_enabled_skills(conn)
        assert [s.name for s in result] == ["on"]


class TestScanSkillsMulti:
    def test_merges_multiple_dirs(self, tmp_path):
        user_dir = tmp_path / "user_skills"
        (user_dir / "alpha").mkdir(parents=True)
        (user_dir / "alpha" / "SKILL.md").write_text("# Alpha\nUser alpha.")

        mod_dir = tmp_path / "mod_skills"
        (mod_dir / "beta").mkdir(parents=True)
        (mod_dir / "beta" / "SKILL.md").write_text("# Beta\nModule beta.")

        result = scan_skills_multi([user_dir, mod_dir])
        assert len(result) == 2
        names = [s.name for s in result]
        assert "alpha" in names
        assert "beta" in names

    def test_first_wins_on_collision(self, tmp_path):
        user_dir = tmp_path / "user"
        (user_dir / "conflict").mkdir(parents=True)
        (user_dir / "conflict" / "SKILL.md").write_text("# Conflict\nUser version.")

        mod_dir = tmp_path / "mod"
        (mod_dir / "conflict").mkdir(parents=True)
        (mod_dir / "conflict" / "SKILL.md").write_text("# Conflict\nModule version.")

        result = scan_skills_multi([user_dir, mod_dir])
        assert len(result) == 1
        assert result[0].description == "User version."

    def test_logs_collision_warning(self, tmp_path, caplog):
        user_dir = tmp_path / "user"
        (user_dir / "dup").mkdir(parents=True)
        (user_dir / "dup" / "SKILL.md").write_text("# Dup\nFirst.")

        mod_dir = tmp_path / "mod"
        (mod_dir / "dup").mkdir(parents=True)
        (mod_dir / "dup" / "SKILL.md").write_text("# Dup\nSecond.")

        scan_skills_multi([user_dir, mod_dir])
        assert "collision" in caplog.text.lower()

    def test_empty_dirs_list(self):
        assert scan_skills_multi([]) == []

    def test_nonexistent_dirs(self, tmp_path):
        result = scan_skills_multi([tmp_path / "nope1", tmp_path / "nope2"])
        assert result == []


class TestGetSkillContent:
    def test_reads_content(self, tmp_path):
        skill_dir = tmp_path / "my_skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("Skill content here.")
        assert get_skill_content("my_skill", tmp_path) == "Skill content here."

    def test_returns_none_for_missing(self, tmp_path):
        assert get_skill_content("nonexistent", tmp_path) is None

    def test_registered_path_takes_priority(self, tmp_path):
        # Workspace skill
        ws_skill = tmp_path / "workspace" / "my_skill"
        ws_skill.mkdir(parents=True)
        (ws_skill / "SKILL.md").write_text("workspace version")
        # Module skill at a different location
        mod_skill = tmp_path / "module" / "my_skill"
        mod_skill.mkdir(parents=True)
        (mod_skill / "SKILL.md").write_text("module version")

        result = get_skill_content("my_skill", tmp_path / "workspace", path=str(mod_skill / "SKILL.md"))
        assert result == "module version"

    def test_falls_back_to_skills_dir_when_path_missing(self, tmp_path):
        skill_dir = tmp_path / "my_skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("fallback content")

        result = get_skill_content("my_skill", tmp_path, path="/nonexistent/SKILL.md")
        assert result == "fallback content"

    def test_returns_none_when_both_missing(self, tmp_path):
        assert get_skill_content("ghost", tmp_path, path="/nonexistent/SKILL.md") is None
