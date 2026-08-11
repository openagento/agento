"""Tests for workspace_build builder logic."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.artifacts_dir import get_current_build_dir
from agento.framework.workspace import AgentView
from agento.modules.workspace_build.src.builder import (
    BuildResult,
    _copy_module_workspaces,
    _copy_theme,
    _create_agents_skills_symlink,
    _read_strategy,
    _write_instruction_files,
    _write_skills_to_build,
    apply_manifest,
    build_manifest,
    compute_build_checksum,
    execute_build,
    materialize_git_identity,
    materialize_ssh_identity,
)

_BUILDER = "agento.modules.workspace_build.src.builder"

pytestmark = pytest.mark.usefixtures("builtin_harnesses")


def _make_agent_view(**overrides):
    defaults = dict(
        id=1, workspace_id=10, code="dev", label="Developer",
        is_active=True, created_at=datetime.now(), updated_at=datetime.now(),
    )
    defaults.update(overrides)
    return AgentView(**defaults)


class TestComputeBuildChecksum:
    def test_deterministic(self):
        overrides = {"a/b": "val1", "c/d": "val2"}
        assert compute_build_checksum(overrides) == compute_build_checksum(overrides)

    def test_changes_with_different_values(self):
        assert compute_build_checksum({"a/b": "val1"}) != compute_build_checksum({"a/b": "val2"})

    def test_changes_with_different_keys(self):
        assert compute_build_checksum({"a/b": "v"}) != compute_build_checksum({"x/y": "v"})

    def test_includes_skill_checksums(self):
        o = {"a/b": "val"}
        assert compute_build_checksum(o) != compute_build_checksum(o, skill_checksums=["abc123"])

    def test_skill_order_irrelevant(self):
        o = {"a/b": "val"}
        assert (
            compute_build_checksum(o, skill_checksums=["aaa", "bbb"])
            == compute_build_checksum(o, skill_checksums=["bbb", "aaa"])
        )

    def test_changes_with_theme_strategy(self):
        o = {"a/b": "val"}
        assert (
            compute_build_checksum(o, strategies={"theme": "copy"})
            != compute_build_checksum(o, strategies={"theme": "symlink"})
        )

    def test_changes_with_modules_strategy(self):
        o = {"a/b": "val"}
        assert (
            compute_build_checksum(o, strategies={"modules": "copy"})
            != compute_build_checksum(o, strategies={"modules": "symlink"})
        )

    def test_changes_with_skills_strategy(self):
        o = {"a/b": "val"}
        assert (
            compute_build_checksum(o, strategies={"skills": "copy"})
            != compute_build_checksum(o, strategies={"skills": "symlink"})
        )

    def test_default_strategies_are_copy(self):
        o = {"a/b": "val"}
        all_copy = {"theme": "copy", "modules": "copy", "skills": "copy"}
        assert compute_build_checksum(o) == compute_build_checksum(o, strategies=all_copy)

    def test_unknown_strategy_source_is_ignored(self):
        """Unknown source keys in strategies dict must not affect the checksum."""
        o = {"a/b": "val"}
        assert (
            compute_build_checksum(o, strategies={"bogus": "symlink"})
            == compute_build_checksum(o)
        )

    def test_includes_skills_layout_marker(self):
        """The _SKILLS_LAYOUT_VERSION marker is mixed into the hash — upgrading the
        on-disk build layout invalidates every pre-existing checksum automatically.
        """
        from agento.modules.workspace_build.src import builder as _b
        o = {"a/b": "val"}
        original = _b._SKILLS_LAYOUT_VERSION
        try:
            _b._SKILLS_LAYOUT_VERSION = "dir_v1"
            checksum_v1 = compute_build_checksum(o)
            _b._SKILLS_LAYOUT_VERSION = "dir_v2"
            checksum_v2 = compute_build_checksum(o)
        finally:
            _b._SKILLS_LAYOUT_VERSION = original
        assert checksum_v1 != checksum_v2

    def test_empty_overrides(self):
        assert len(compute_build_checksum({})) == 64

    def test_returns_sha256_hex(self):
        checksum = compute_build_checksum({"x": "y"})
        assert len(checksum) == 64
        assert all(c in "0123456789abcdef" for c in checksum)


class TestBuildManifest:
    """Tests for the recursive merge-walk that produces a layered manifest."""

    def test_unique_names_keep_as_is(self, tmp_path):
        base = tmp_path / "base"
        ws = tmp_path / "ws"
        base.mkdir()
        ws.mkdir()
        (base / "only_in_base.md").write_text("b")
        (ws / "only_in_ws.md").write_text("w")
        (base / "base_dir").mkdir()
        (base / "base_dir" / "x.md").write_text("x")

        manifest = build_manifest([base, ws])
        assert set(manifest.keys()) == {"only_in_base.md", "only_in_ws.md", "base_dir"}
        assert manifest["only_in_base.md"] == (base / "only_in_base.md", "file")
        assert manifest["only_in_ws.md"] == (ws / "only_in_ws.md", "file")
        assert manifest["base_dir"] == (base / "base_dir", "dir")

    def test_file_collision_latest_wins(self, tmp_path):
        base = tmp_path / "base"
        ws = tmp_path / "ws"
        av = tmp_path / "av"
        for d in (base, ws, av):
            d.mkdir()
        (base / "file.md").write_text("base")
        (ws / "file.md").write_text("ws")
        (av / "file.md").write_text("av")

        manifest = build_manifest([base, ws, av])
        assert manifest["file.md"] == (av / "file.md", "file")

    def test_dir_collision_descends(self, tmp_path):
        base = tmp_path / "base"
        ws = tmp_path / "ws"
        (base / "docs").mkdir(parents=True)
        (base / "docs" / "a.md").write_text("a")
        (ws / "docs").mkdir(parents=True)
        (ws / "docs" / "b.md").write_text("b")

        manifest = build_manifest([base, ws])
        assert set(manifest.keys()) == {"docs/a.md", "docs/b.md"}
        assert manifest["docs/a.md"] == (base / "docs" / "a.md", "file")
        assert manifest["docs/b.md"] == (ws / "docs" / "b.md", "file")

    def test_dir_collision_descends_with_nested_collision(self, tmp_path):
        base = tmp_path / "base"
        ws = tmp_path / "ws"
        (base / "docs" / "inner").mkdir(parents=True)
        (base / "docs" / "inner" / "x.md").write_text("base-inner")
        (ws / "docs" / "inner").mkdir(parents=True)
        (ws / "docs" / "inner" / "x.md").write_text("ws-inner")
        (ws / "docs" / "inner" / "y.md").write_text("ws-only")

        manifest = build_manifest([base, ws])
        # x.md collides at depth 2 → latest wins
        assert manifest["docs/inner/x.md"] == (ws / "docs" / "inner" / "x.md", "file")
        # y.md unique at depth 2 → stays
        assert manifest["docs/inner/y.md"] == (ws / "docs" / "inner" / "y.md", "file")

    def test_mixed_file_and_dir_latest_wins(self, tmp_path):
        """file in one layer, dir with same name in another → latest wins wholesale, no descent."""
        base = tmp_path / "base"
        ws = tmp_path / "ws"
        base.mkdir()
        (base / "notes.md").write_text("base file")
        (ws / "notes.md").mkdir(parents=True)
        (ws / "notes.md" / "draft.md").write_text("ws draft")

        manifest = build_manifest([base, ws])
        # ws is later → the directory wins
        assert manifest["notes.md"] == (ws / "notes.md", "dir")
        # no descended children since collision was mixed
        assert not any(k.startswith("notes.md/") for k in manifest)

    def test_skips_dot_and_underscore_prefixed(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        (base / "visible.md").write_text("v")
        (base / ".hidden").write_text("h")
        (base / "_scope").mkdir()
        (base / "_scope" / "inside.md").write_text("s")

        manifest = build_manifest([base])
        assert "visible.md" in manifest
        assert ".hidden" not in manifest
        assert "_scope" not in manifest
        assert not any("inside.md" in k for k in manifest)

    def test_missing_or_none_layers_skipped(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        (base / "f.md").write_text("x")

        manifest = build_manifest([base, None, tmp_path / "nonexistent"])
        assert set(manifest.keys()) == {"f.md"}

    def test_max_depth_cap(self, tmp_path, monkeypatch):
        """At MAX_DEPTH, colliding dirs collapse to latest-layer wins (no further descent)."""
        import agento.modules.workspace_build.src.builder as builder_mod
        monkeypatch.setattr(builder_mod, "_MAX_MANIFEST_DEPTH", 1)

        base = tmp_path / "base"
        ws = tmp_path / "ws"
        (base / "a" / "b" / "c.md").parent.mkdir(parents=True)
        (base / "a" / "b" / "c.md").write_text("base")
        (ws / "a" / "b" / "c.md").parent.mkdir(parents=True)
        (ws / "a" / "b" / "c.md").write_text("ws")
        (ws / "a" / "b" / "extra.md").write_text("ws-extra")

        manifest = build_manifest([base, ws])
        # depth 0: 'a' collides (both dirs) → descend to depth 1
        # depth 1: 'b' collides (both dirs) but depth >= MAX (1) → latest wins on b wholesale
        assert manifest.get("a/b") == (ws / "a" / "b", "dir")
        # No descended children — cap prevented depth-2 descent
        assert not any(k.startswith("a/b/") for k in manifest)

    def test_empty_layers_returns_empty(self):
        assert build_manifest([]) == {}


class TestApplyManifest:
    """Tests for the strategy-aware writer that materializes a manifest on disk."""

    def test_copy_files_and_dirs(self, tmp_path):
        src_file = tmp_path / "src_file.md"
        src_file.write_text("hello")
        src_dir = tmp_path / "src_dir"
        src_dir.mkdir()
        (src_dir / "inner.md").write_text("inner")

        target = tmp_path / "target"
        manifest = {"f.md": (src_file, "file"), "d": (src_dir, "dir")}
        apply_manifest(manifest, target, "copy")

        assert (target / "f.md").is_file()
        assert not (target / "f.md").is_symlink()
        assert (target / "f.md").read_text() == "hello"
        assert (target / "d").is_dir()
        assert not (target / "d").is_symlink()
        assert (target / "d" / "inner.md").read_text() == "inner"

    def test_symlink_files_and_dirs(self, tmp_path):
        src_file = tmp_path / "src_file.md"
        src_file.write_text("hello")
        src_dir = tmp_path / "src_dir"
        src_dir.mkdir()
        (src_dir / "inner.md").write_text("inner")

        target = tmp_path / "target"
        manifest = {"f.md": (src_file, "file"), "d": (src_dir, "dir")}
        apply_manifest(manifest, target, "symlink")

        assert (target / "f.md").is_symlink()
        assert (target / "f.md").read_text() == "hello"
        assert (target / "d").is_symlink()
        assert (target / "d" / "inner.md").read_text() == "inner"

    def test_symlink_target_is_absolute(self, tmp_path):
        src_file = tmp_path / "src.md"
        src_file.write_text("x")
        target = tmp_path / "out"
        apply_manifest({"f.md": (src_file, "file")}, target, "symlink")

        link = target / "f.md"
        assert link.is_symlink()
        assert str(link.readlink()).startswith(str(tmp_path.resolve()))

    def test_nested_relative_paths_create_real_parent_dirs(self, tmp_path):
        src = tmp_path / "src.md"
        src.write_text("nested")
        target = tmp_path / "out"
        apply_manifest({"a/b/c.md": (src, "file")}, target, "symlink")

        parent = target / "a" / "b"
        assert parent.is_dir()
        assert not parent.is_symlink()
        assert (parent / "c.md").is_symlink()

    def test_reapply_replaces_prior_entry(self, tmp_path):
        """Re-running apply_manifest on the same target replaces stale entries cleanly."""
        target = tmp_path / "out"
        src_a = tmp_path / "a.md"
        src_a.write_text("A")
        src_b = tmp_path / "b.md"
        src_b.write_text("B")

        apply_manifest({"f.md": (src_a, "file")}, target, "copy")
        assert (target / "f.md").read_text() == "A"

        apply_manifest({"f.md": (src_b, "file")}, target, "symlink")
        assert (target / "f.md").is_symlink()
        assert (target / "f.md").read_text() == "B"


class TestCopyTheme:
    """Tests for the flat _copy_theme cascade (THEME_DIR \u2192 _{ws} \u2192 _{ws}/_{av})."""

    def test_base_layer_from_theme_dir(self, tmp_path):
        theme = tmp_path / "theme"
        theme.mkdir()
        (theme / "SOUL.md").write_text("# Base soul")
        (theme / "app").mkdir()
        (theme / "app" / "data.txt").write_text("base data")

        build = tmp_path / "build"
        build.mkdir()
        with patch(f"{_BUILDER}.THEME_DIR", str(theme)):
            _copy_theme(build, "default", "agent01")
        assert (build / "SOUL.md").read_text() == "# Base soul"
        assert (build / "app" / "data.txt").read_text() == "base data"

    def test_workspace_layer_overrides_base(self, tmp_path):
        theme = tmp_path / "theme"
        theme.mkdir()
        (theme / "SOUL.md").write_text("# Base soul")
        ws = theme / "_myws"
        ws.mkdir()
        (ws / "SOUL.md").write_text("# Workspace soul")

        build = tmp_path / "build"
        build.mkdir()
        with patch(f"{_BUILDER}.THEME_DIR", str(theme)):
            _copy_theme(build, "myws", "dev01")
        assert (build / "SOUL.md").read_text() == "# Workspace soul"

    def test_agent_view_layer_overrides_workspace(self, tmp_path):
        theme = tmp_path / "theme"
        theme.mkdir()
        (theme / "SOUL.md").write_text("# Base")
        ws = theme / "_myws"
        ws.mkdir()
        (ws / "SOUL.md").write_text("# Workspace")
        av = ws / "_dev01"
        av.mkdir()
        (av / "SOUL.md").write_text("# Agent view")

        build = tmp_path / "build"
        build.mkdir()
        with patch(f"{_BUILDER}.THEME_DIR", str(theme)):
            _copy_theme(build, "myws", "dev01")
        assert (build / "SOUL.md").read_text() == "# Agent view"

    def test_scope_dirs_not_copied_as_content(self, tmp_path):
        theme = tmp_path / "theme"
        theme.mkdir()
        (theme / "visible.md").write_text("yes")
        (theme / "_ws1").mkdir()
        (theme / "_ws1" / "scoped.md").write_text("scoped")

        build = tmp_path / "build"
        build.mkdir()
        with patch(f"{_BUILDER}.THEME_DIR", str(theme)):
            _copy_theme(build, "other", "dev")
        assert (build / "visible.md").exists()
        assert not (build / "_ws1").exists()

    def test_base_plus_workspace_no_agent_view(self, tmp_path):
        theme = tmp_path / "theme"
        theme.mkdir()
        (theme / "base.md").write_text("base")
        ws = theme / "_myws"
        ws.mkdir()
        (ws / "ws.md").write_text("ws extra")

        build = tmp_path / "build"
        build.mkdir()
        with patch(f"{_BUILDER}.THEME_DIR", str(theme)):
            _copy_theme(build, "myws", "nonexistent_av")
        assert (build / "base.md").read_text() == "base"
        assert (build / "ws.md").read_text() == "ws extra"

    def test_directory_merge_across_layers(self, tmp_path):
        """Subdirectories merge rather than replace across layers."""
        theme = tmp_path / "theme"
        (theme / "docs").mkdir(parents=True)
        (theme / "docs" / "base.md").write_text("base doc")
        ws = theme / "_myws"
        (ws / "docs").mkdir(parents=True)
        (ws / "docs" / "ws.md").write_text("ws doc")

        build = tmp_path / "build"
        build.mkdir()
        with patch(f"{_BUILDER}.THEME_DIR", str(theme)):
            _copy_theme(build, "myws", "dev")
        assert (build / "docs" / "base.md").read_text() == "base doc"
        assert (build / "docs" / "ws.md").read_text() == "ws doc"

    def test_dot_prefixed_items_skipped(self, tmp_path):
        """Dot-prefixed items at theme root aren't copied to builds."""
        theme = tmp_path / "theme"
        theme.mkdir()
        (theme / "included.md").write_text("in")
        (theme / ".hidden").mkdir()
        (theme / ".hidden" / "secret.md").write_text("should not leak")

        build = tmp_path / "build"
        build.mkdir()
        with patch(f"{_BUILDER}.THEME_DIR", str(theme)):
            _copy_theme(build, "default", "agent01")
        assert (build / "included.md").exists()
        assert not (build / ".hidden").exists()

    def test_noop_when_theme_missing(self, tmp_path):
        build = tmp_path / "build"
        build.mkdir()
        with patch(f"{_BUILDER}.THEME_DIR", str(tmp_path / "nonexistent")):
            _copy_theme(build, "default", "agent01")
        assert list(build.iterdir()) == []

    def test_symlink_strategy_preserves_layer_override(self, tmp_path):
        """Heavy base dir stays symlinked; colliding subtrees descend to file-level symlinks."""
        theme = tmp_path / "theme"
        (theme / "heavy").mkdir(parents=True)
        (theme / "heavy" / "bulk.bin").write_text("x" * 1024)
        (theme / "docs").mkdir()
        (theme / "docs" / "base.md").write_text("base")
        ws = theme / "_myws"
        (ws / "docs").mkdir(parents=True)
        (ws / "docs" / "ws.md").write_text("ws-only")

        build = tmp_path / "build"
        build.mkdir()
        with patch(f"{_BUILDER}.THEME_DIR", str(theme)):
            _copy_theme(build, "myws", "dev", strategy="symlink")

        # Unique base dir → single symlink
        assert (build / "heavy").is_symlink()
        assert (build / "heavy" / "bulk.bin").read_text() == "x" * 1024
        # Colliding dir → real dir, file-level symlinks underneath
        assert (build / "docs").is_dir()
        assert not (build / "docs").is_symlink()
        assert (build / "docs" / "base.md").is_symlink()
        assert (build / "docs" / "ws.md").is_symlink()


class TestCopyModuleWorkspaces:
    """Tests for _copy_module_workspaces under both strategies."""

    def _make_manifest(self, tmp_path, name="testmod"):
        mod_dir = tmp_path / "modules" / name
        ws_dir = mod_dir / "workspace"
        ws_dir.mkdir(parents=True)
        (ws_dir / "README.md").write_text("# Test knowledge")
        (ws_dir / "subdir").mkdir()
        (ws_dir / "subdir" / "deep.md").write_text("deep file")
        manifest = MagicMock()
        manifest.name = name
        manifest.path = str(mod_dir)
        return manifest

    def _make_layered_manifest(self, tmp_path, name="layered_mod"):
        mod_dir = tmp_path / "modules" / name
        ws_dir = mod_dir / "workspace"
        ws_dir.mkdir(parents=True)
        (ws_dir / "base.md").write_text("# Base")
        scope = ws_dir / "_myws"
        scope.mkdir()
        (scope / "ws.md").write_text("# WS scoped")
        av = scope / "_dev01"
        av.mkdir()
        (av / "av.md").write_text("# AV scoped")
        manifest = MagicMock()
        manifest.name = name
        manifest.path = str(mod_dir)
        return manifest

    @patch("agento.framework.bootstrap.get_manifests")
    def test_copy_strategy_copies_files(self, mock_get_manifests, tmp_path):
        manifest = self._make_manifest(tmp_path)
        mock_get_manifests.return_value = [manifest]

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        _copy_module_workspaces(build_dir, "default", "agent01", strategy="copy")

        dest = build_dir / "modules" / "testmod"
        assert dest.is_dir()
        assert not dest.is_symlink()
        assert (dest / "README.md").read_text() == "# Test knowledge"
        assert not (dest / "README.md").is_symlink()
        assert (dest / "subdir" / "deep.md").read_text() == "deep file"

    @patch("agento.framework.bootstrap.get_manifests")
    def test_symlink_strategy_symlinks_top_level_items(self, mock_get_manifests, tmp_path):
        """Symlink strategy creates one symlink per top-level item under a real dest dir."""
        manifest = self._make_manifest(tmp_path)
        mock_get_manifests.return_value = [manifest]

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        _copy_module_workspaces(build_dir, "default", "agent01", strategy="symlink")

        dest = build_dir / "modules" / "testmod"
        assert dest.is_dir()
        assert not dest.is_symlink()
        assert (dest / "README.md").is_symlink()
        assert (dest / "README.md").read_text() == "# Test knowledge"
        # Subdir becomes its own symlink (no collision)
        assert (dest / "subdir").is_symlink()
        assert (dest / "subdir" / "deep.md").read_text() == "deep file"

    @patch("agento.framework.bootstrap.get_manifests")
    def test_default_strategy_is_copy(self, mock_get_manifests, tmp_path):
        manifest = self._make_manifest(tmp_path)
        mock_get_manifests.return_value = [manifest]

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        _copy_module_workspaces(build_dir, "default", "agent01")

        dest = build_dir / "modules" / "testmod"
        assert (dest / "README.md").is_file()
        assert not (dest / "README.md").is_symlink()

    @patch("agento.framework.bootstrap.get_manifests")
    def test_multiple_modules_materialized_independently(self, mock_get_manifests, tmp_path):
        m1 = self._make_manifest(tmp_path, "mod_a")
        m2 = self._make_manifest(tmp_path, "mod_b")
        mock_get_manifests.return_value = [m1, m2]

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        _copy_module_workspaces(build_dir, "default", "agent01", strategy="symlink")

        assert (build_dir / "modules" / "mod_a" / "README.md").is_symlink()
        assert (build_dir / "modules" / "mod_b" / "README.md").is_symlink()

    @patch("agento.framework.bootstrap.get_manifests")
    def test_skips_module_without_workspace_dir(self, mock_get_manifests, tmp_path):
        mod_dir = tmp_path / "modules" / "empty_mod"
        mod_dir.mkdir(parents=True)
        manifest = MagicMock()
        manifest.name = "empty_mod"
        manifest.path = str(mod_dir)
        mock_get_manifests.return_value = [manifest]

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        _copy_module_workspaces(build_dir, "default", "agent01", strategy="symlink")

        assert not (build_dir / "modules" / "empty_mod").exists()

    @patch("agento.framework.bootstrap.get_manifests")
    def test_layered_copy_with_scope_dirs(self, mock_get_manifests, tmp_path):
        """Module with _* dirs applies layered copy: base + workspace + agent_view."""
        manifest = self._make_layered_manifest(tmp_path)
        mock_get_manifests.return_value = [manifest]

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        _copy_module_workspaces(build_dir, "myws", "dev01", strategy="copy")

        dest = build_dir / "modules" / "layered_mod"
        assert (dest / "base.md").read_text() == "# Base"
        assert (dest / "ws.md").read_text() == "# WS scoped"
        assert (dest / "av.md").read_text() == "# AV scoped"
        # Scope dirs must not appear in output
        assert not (dest / "_myws").exists()

    @patch("agento.framework.bootstrap.get_manifests")
    def test_layered_symlink_preserves_overrides(self, mock_get_manifests, tmp_path):
        """Symlink strategy with scope overlays symlinks each layer's unique files."""
        manifest = self._make_layered_manifest(tmp_path)
        mock_get_manifests.return_value = [manifest]

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        _copy_module_workspaces(build_dir, "myws", "dev01", strategy="symlink")

        dest = build_dir / "modules" / "layered_mod"
        assert dest.is_dir()
        assert not dest.is_symlink()
        assert (dest / "base.md").is_symlink()
        assert (dest / "base.md").read_text() == "# Base"
        assert (dest / "ws.md").is_symlink()
        assert (dest / "ws.md").read_text() == "# WS scoped"
        assert (dest / "av.md").is_symlink()
        assert (dest / "av.md").read_text() == "# AV scoped"

    @patch("agento.framework.bootstrap.get_manifests")
    def test_layered_workspace_only_no_agent_view(self, mock_get_manifests, tmp_path):
        """When workspace matches but agent_view doesn't, only base + ws layers apply."""
        manifest = self._make_layered_manifest(tmp_path)
        mock_get_manifests.return_value = [manifest]

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        _copy_module_workspaces(build_dir, "myws", "other_av", strategy="copy")

        dest = build_dir / "modules" / "layered_mod"
        assert (dest / "base.md").read_text() == "# Base"
        assert (dest / "ws.md").read_text() == "# WS scoped"
        assert not (dest / "av.md").exists()

    @patch("agento.framework.bootstrap.get_manifests")
    def test_layered_no_matching_workspace(self, mock_get_manifests, tmp_path):
        """When workspace doesn't match, only base layer applies."""
        manifest = self._make_layered_manifest(tmp_path)
        mock_get_manifests.return_value = [manifest]

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        _copy_module_workspaces(build_dir, "other_ws", "dev01", strategy="copy")

        dest = build_dir / "modules" / "layered_mod"
        assert (dest / "base.md").read_text() == "# Base"
        assert not (dest / "ws.md").exists()
        assert not (dest / "av.md").exists()

    @patch("agento.framework.bootstrap.get_manifests")
    def test_user_module_overlay_merges_via_app_code_path(self, mock_get_manifests, tmp_path):
        """User modules (app/code/*) apply the layered cascade exactly like core modules."""
        # Simulate a manifest whose path lives outside src/agento/modules — i.e. app/code/*.
        mod_dir = tmp_path / "app" / "code" / "my_module"
        ws_dir = mod_dir / "workspace"
        ws_dir.mkdir(parents=True)
        (ws_dir / "README.md").write_text("# base readme")
        scope = ws_dir / "_myws"
        scope.mkdir()
        (scope / "only_in_ws.md").write_text("ws only")
        (scope / "README.md").write_text("# ws readme override")
        av = scope / "_myav"
        av.mkdir()
        (av / "only_av.md").write_text("av only")

        manifest = MagicMock()
        manifest.name = "my_module"
        manifest.path = str(mod_dir)
        mock_get_manifests.return_value = [manifest]

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        _copy_module_workspaces(build_dir, "myws", "myav", strategy="copy")

        dest = build_dir / "modules" / "my_module"
        assert dest.is_dir()
        assert (dest / "README.md").read_text() == "# ws readme override"
        assert (dest / "only_in_ws.md").read_text() == "ws only"
        assert (dest / "only_av.md").read_text() == "av only"
        # Scope dirs must not appear as content.
        assert not (dest / "_myws").exists()


class TestReadStrategy:
    """Tests for _read_strategy: reads global scope only, falls back to config.json, coerces invalid values."""

    def _mock_conn_for_global_overrides(self, rows):
        """Build a mock conn whose global-scope query returns the given rows."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cursor.fetchall.return_value = rows
        return conn

    def test_reads_global_db_value(self):
        rows = [{"path": "workspace_build/strategy/theme", "value": "symlink", "encrypted": 0}]
        conn = self._mock_conn_for_global_overrides(rows)
        with patch("agento.framework.bootstrap.get_module_config", return_value={}):
            assert _read_strategy(conn, "theme") == "symlink"

    def test_falls_back_to_module_config(self):
        conn = self._mock_conn_for_global_overrides([])
        with patch(
            "agento.framework.bootstrap.get_module_config",
            return_value={"strategy/modules": "symlink"},
        ):
            assert _read_strategy(conn, "modules") == "symlink"

    def test_defaults_to_copy_when_unset(self):
        conn = self._mock_conn_for_global_overrides([])
        with patch("agento.framework.bootstrap.get_module_config", return_value={}):
            assert _read_strategy(conn, "skills") == "copy"

    def test_invalid_value_falls_back_to_copy(self):
        rows = [{"path": "workspace_build/strategy/theme", "value": "bogus", "encrypted": 0}]
        conn = self._mock_conn_for_global_overrides(rows)
        with patch("agento.framework.bootstrap.get_module_config", return_value={}):
            assert _read_strategy(conn, "theme") == "copy"

    def test_ignores_scoped_db_overrides(self):
        """A value set at workspace or agent_view scope must not influence _read_strategy."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        def fetchall_side_effect():
            # load_scoped_db_overrides queries (scope=default, scope_id=0) only;
            # we return nothing to simulate global scope being unset.
            return []
        cursor.fetchall.side_effect = fetchall_side_effect

        with patch("agento.framework.bootstrap.get_module_config", return_value={}):
            assert _read_strategy(conn, "theme") == "copy"

        # Confirm we queried only the global scope
        queries = [c.args for c in cursor.execute.call_args_list]
        for sql, params in queries:
            if "core_config_data" in sql and "scope" in sql:
                # load_scoped_db_overrides passes (scope, scope_id) as params
                assert params == ("default", 0)

    def test_rejects_unknown_source(self):
        conn = MagicMock()
        with pytest.raises(ValueError, match="Unknown workspace_build source"):
            _read_strategy(conn, "bogus")


class TestWriteInstructionFiles:
    def test_writes_from_overrides(self, tmp_path):
        overrides = {
            "agent_view/instructions/agents_md": "# My agents instructions",
            "agent_view/instructions/soul_md": "# Soul content",
        }
        _write_instruction_files(tmp_path, overrides)
        assert (tmp_path / "AGENTS.md").read_text() == "# My agents instructions"
        assert (tmp_path / "SOUL.md").read_text() == "# Soul content"
        assert (tmp_path / "CLAUDE.md").exists()

    def test_always_writes_claude_md(self, tmp_path):
        _write_instruction_files(tmp_path, {})
        assert "AGENTS.md" in (tmp_path / "CLAUDE.md").read_text()

    def test_skips_empty_override_value(self, tmp_path):
        _write_instruction_files(tmp_path, {"agent_view/instructions/agents_md": ""})
        assert not (tmp_path / "AGENTS.md").exists()

    def test_does_not_overwrite_theme_file_without_db_value(self, tmp_path):
        """When no DB override exists, theme file (already in build_dir) is preserved."""
        (tmp_path / "SOUL.md").write_text("# From theme")
        _write_instruction_files(tmp_path, {})
        assert (tmp_path / "SOUL.md").read_text() == "# From theme"

    def test_db_override_replaces_theme_file(self, tmp_path):
        """DB override takes precedence over file already in build_dir from theme."""
        (tmp_path / "SOUL.md").write_text("# From theme")
        _write_instruction_files(
            tmp_path, {"agent_view/instructions/soul_md": "# From DB"},
        )
        assert (tmp_path / "SOUL.md").read_text() == "# From DB"

    def test_unlinks_symlink_before_writing(self, tmp_path):
        """If a prior layer left AGENTS.md as a symlink, writing must not mutate the source."""
        source_dir = tmp_path / "theme_src"
        source_dir.mkdir()
        source_file = source_dir / "AGENTS.md"
        source_file.write_text("# original source content")

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        (build_dir / "AGENTS.md").symlink_to(source_file)

        _write_instruction_files(
            build_dir,
            {"agent_view/instructions/agents_md": "# from DB"},
        )
        # Source file must be unchanged
        assert source_file.read_text() == "# original source content"
        # Build file must contain the DB value, and no longer be a symlink
        target = build_dir / "AGENTS.md"
        assert target.read_text() == "# from DB"
        assert not target.is_symlink()

    def test_unlinks_symlinked_claude_md_before_writing(self, tmp_path):
        """CLAUDE.md is always overwritten — must not follow a prior symlink."""
        source_dir = tmp_path / "theme_src"
        source_dir.mkdir()
        source_file = source_dir / "CLAUDE.md"
        source_file.write_text("# original claude content")

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        (build_dir / "CLAUDE.md").symlink_to(source_file)

        _write_instruction_files(build_dir, {})
        # Source file must be unchanged
        assert source_file.read_text() == "# original claude content"
        # CLAUDE.md must be the canonical pointer content
        assert not (build_dir / "CLAUDE.md").is_symlink()
        assert "AGENTS.md" in (build_dir / "CLAUDE.md").read_text()


class TestMaterializeSshIdentity:
    def test_writes_key_config_known_hosts_from_resolved(self, tmp_path):
        resolved = {
            "agent_view/identity/ssh_private_key": "PRIVATE-KEY",
            "agent_view/identity/ssh_public_key": "PUBLIC-KEY",
            "agent_view/identity/ssh_config": "Host github.com",
            "agent_view/identity/ssh_known_hosts": "github.com ssh-ed25519 AAA",
        }
        materialize_ssh_identity(tmp_path, resolved)
        ssh = tmp_path / ".ssh"
        assert ssh.stat().st_mode & 0o777 == 0o700
        key = ssh / "id_rsa"
        assert key.read_text() == "PRIVATE-KEY\n"
        assert key.stat().st_mode & 0o777 == 0o600
        assert (ssh / "id_rsa.pub").read_text() == "PUBLIC-KEY"
        assert (ssh / "config").read_text() == "Host github.com"
        assert (ssh / "known_hosts").read_text() == "github.com ssh-ed25519 AAA"

    def test_noop_when_no_identity(self, tmp_path):
        materialize_ssh_identity(tmp_path, {"agent_view/model": "opus"})
        assert not (tmp_path / ".ssh").exists()


class TestMaterializeGitIdentity:
    _N = "agent_view/identity/git_author_name"
    _E = "agent_view/identity/git_author_email"

    def test_writes_gitconfig_from_resolved(self, tmp_path):
        materialize_git_identity(tmp_path, {self._N: "Example User", self._E: "agent@example.com"})
        assert (tmp_path / ".gitconfig").read_text() == (
            '[user]\n\tname = "Example User"\n\temail = "agent@example.com"\n'
        )

    def test_writes_email_only(self, tmp_path):
        materialize_git_identity(tmp_path, {self._E: "agent@example.com"})
        assert (tmp_path / ".gitconfig").read_text() == '[user]\n\temail = "agent@example.com"\n'

    def test_writes_name_only(self, tmp_path):
        materialize_git_identity(tmp_path, {self._N: "Example"})
        assert (tmp_path / ".gitconfig").read_text() == '[user]\n\tname = "Example"\n'

    def test_noop_when_no_identity(self, tmp_path):
        materialize_git_identity(tmp_path, {"agent_view/model": "opus"})
        assert not (tmp_path / ".gitconfig").exists()

    def test_noop_when_only_whitespace_or_control_chars(self, tmp_path):
        # Values that clean to empty are treated as absent — no file written.
        materialize_git_identity(tmp_path, {self._N: "  \t ", self._E: "\n\r\x00"})
        assert not (tmp_path / ".gitconfig").exists()

    def test_strips_whitespace(self, tmp_path):
        materialize_git_identity(tmp_path, {self._N: "  Example  "})
        assert (tmp_path / ".gitconfig").read_text() == '[user]\n\tname = "Example"\n'

    def test_escapes_quotes_and_backslash(self, tmp_path):
        materialize_git_identity(tmp_path, {self._N: 'Foo\\bar"baz'})
        assert (tmp_path / ".gitconfig").read_text() == '[user]\n\tname = "Foo\\\\bar\\"baz"\n'

    def test_neutralizes_gitconfig_injection(self, tmp_path):
        # A newline-laden value must NOT inject a new [core] section / sshCommand directive: the
        # control chars are stripped and the residue is confined to a single double-quoted value line.
        evil = "Evil\n[core]\n\tsshCommand = touch /pwned"
        materialize_git_identity(tmp_path, {self._N: evil, self._E: "agent@example.com\x00"})
        content = (tmp_path / ".gitconfig").read_text()
        lines = content.splitlines()
        # Exactly one section header, and it is [user].
        assert [ln for ln in lines if ln.startswith("[")] == ["[user]"]
        # No standalone injected section / directive lines.
        assert not any(ln.strip() == "[core]" for ln in lines)
        assert not any(ln.strip().startswith("sshCommand") for ln in lines)
        # No raw newline/NUL survived inside the value lines.
        assert "\x00" not in content
        # The name value is a single double-quoted line containing the collapsed residue.
        assert '\tname = "Evil[core]\tsshCommand = touch /pwned"' in lines or \
               '\tname = "Evil[core]sshCommand = touch /pwned"' in lines
        # Optional: if git is available, confirm it reads the cleaned value and finds NO injection.
        import shutil as _sh
        import subprocess
        if _sh.which("git"):
            f = str(tmp_path / ".gitconfig")
            name = subprocess.run(["git", "config", "--file", f, "--get", "user.name"],
                                  capture_output=True, text=True).stdout.strip()
            assert name and "\n" not in name and name != evil  # cleaned, single-line, not raw
            inj = subprocess.run(["git", "config", "--file", f, "--get", "core.sshCommand"],
                                 capture_output=True, text=True)
            assert inj.returncode != 0 and inj.stdout.strip() == ""  # no injected directive


class TestWriteSkillsToBuild:
    """Skills must materialize as directories (SKILL.md + companion files),
    not as flat single-file Markdown. Claude Code expects
    .claude/skills/<name>/SKILL.md with any references/scripts alongside."""

    def _make_skill(self, tmp_path, name, extra_files=()):
        skill_dir = tmp_path / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {name}")
        for rel, body in extra_files:
            target = skill_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        skill = MagicMock()
        skill.name = name
        skill.path = str(skill_dir / "SKILL.md")
        return skill, skill_dir

    def test_copies_skill_directory_with_skill_md(self, tmp_path):
        skill, _ = self._make_skill(tmp_path, "my_skill")
        build = tmp_path / "build"
        build.mkdir()
        _write_skills_to_build(build, [skill], registry=MagicMock(), skills_dir=tmp_path / "skills")

        dest = build / ".claude" / "skills" / "my_skill"
        assert dest.is_dir()
        assert not dest.is_symlink()
        assert (dest / "SKILL.md").read_text() == "# my_skill"
        assert not (build / ".claude" / "skills" / "my_skill.md").exists()

    def test_symlink_strategy_creates_skill_dir_symlinks(self, tmp_path):
        skill, _ = self._make_skill(tmp_path, "my_skill")
        build = tmp_path / "build"
        build.mkdir()
        _write_skills_to_build(
            build, [skill], registry=MagicMock(), skills_dir=tmp_path / "skills",
            strategy="symlink",
        )

        dest = build / ".claude" / "skills" / "my_skill"
        assert dest.is_symlink()
        assert (dest / "SKILL.md").read_text() == "# my_skill"

    def test_preserves_companion_files(self, tmp_path):
        skill, _ = self._make_skill(
            tmp_path, "mysql_k3",
            extra_files=[
                ("references/schema.md", "# schema ref"),
                ("scripts/run.sh", "#!/bin/bash\necho hi\n"),
            ],
        )
        build = tmp_path / "build"
        build.mkdir()
        _write_skills_to_build(build, [skill], registry=MagicMock(), skills_dir=tmp_path / "skills")

        dest = build / ".claude" / "skills" / "mysql_k3"
        assert (dest / "SKILL.md").exists()
        assert (dest / "references" / "schema.md").read_text() == "# schema ref"
        assert (dest / "scripts" / "run.sh").read_text().startswith("#!/bin/bash")

    def test_falls_back_to_skills_dir_when_path_missing(self, tmp_path):
        """skill.path is stale (file deleted) — fall back to skills_dir / name."""
        skills_dir = tmp_path / "skills"
        fallback = skills_dir / "fallback_skill"
        fallback.mkdir(parents=True)
        (fallback / "SKILL.md").write_text("# fallback")

        skill = MagicMock()
        skill.name = "fallback_skill"
        skill.path = str(tmp_path / "nonexistent" / "SKILL.md")

        build = tmp_path / "build"
        build.mkdir()
        _write_skills_to_build(build, [skill], registry=MagicMock(), skills_dir=skills_dir)

        assert (build / ".claude" / "skills" / "fallback_skill" / "SKILL.md").read_text() == "# fallback"

    def test_skips_skill_when_source_dir_missing(self, tmp_path, caplog):
        """Neither skill.path parent nor skills_dir/name exists — skip with warning, no crash."""
        skill = MagicMock()
        skill.name = "ghost"
        skill.path = str(tmp_path / "missing" / "SKILL.md")

        build = tmp_path / "build"
        build.mkdir()
        _write_skills_to_build(build, [skill], registry=MagicMock(), skills_dir=tmp_path / "skills")

        assert not (build / ".claude" / "skills" / "ghost").exists()

    def test_no_output_when_skills_empty(self, tmp_path):
        build = tmp_path / "build"
        build.mkdir()
        _write_skills_to_build(build, [], registry=MagicMock(), skills_dir=tmp_path / "skills")
        assert not (build / ".claude").exists()

    def test_no_output_when_registry_none(self, tmp_path):
        """Soft-dependency: skill module not loaded → skills param may be empty, registry None."""
        build = tmp_path / "build"
        build.mkdir()
        _write_skills_to_build(build, [MagicMock()], registry=None, skills_dir=tmp_path / "skills")
        assert not (build / ".claude").exists()


class TestCreateAgentsSkillsSymlink:
    def test_creates_symlink_when_claude_skills_exists(self, tmp_path):
        build = tmp_path / "build"
        claude_skills = build / ".claude" / "skills"
        claude_skills.mkdir(parents=True)
        _create_agents_skills_symlink(build)
        symlink = build / ".agents" / "skills"
        assert symlink.is_symlink()
        assert symlink.resolve() == claude_skills.resolve()

    def test_no_op_when_claude_skills_missing(self, tmp_path):
        build = tmp_path / "build"
        build.mkdir()
        _create_agents_skills_symlink(build)
        assert not (build / ".agents").exists()

    def test_replaces_existing_symlink(self, tmp_path):
        build = tmp_path / "build"
        claude_skills = build / ".claude" / "skills"
        claude_skills.mkdir(parents=True)
        agents_dir = build / ".agents"
        agents_dir.mkdir()
        old_target = tmp_path / "old"
        old_target.mkdir()
        (agents_dir / "skills").symlink_to(old_target)
        _create_agents_skills_symlink(build)
        assert (agents_dir / "skills").resolve() == claude_skills.resolve()

    def test_symlink_is_relative(self, tmp_path):
        build = tmp_path / "build"
        claude_skills = build / ".claude" / "skills"
        claude_skills.mkdir(parents=True)
        _create_agents_skills_symlink(build)
        symlink = build / ".agents" / "skills"
        assert not symlink.readlink().is_absolute()


class TestGetCurrentBuildDir:
    _ARTIFACTS_DIR = "agento.framework.artifacts_dir"

    def test_returns_none_when_no_symlink(self, tmp_path):
        with patch(f"{self._ARTIFACTS_DIR}.BUILD_DIR", str(tmp_path)):
            assert get_current_build_dir("ws", "av") is None

    def test_returns_path_when_symlink_exists(self, tmp_path):
        build_dir = tmp_path / "ws" / "av" / "builds" / "1"
        build_dir.mkdir(parents=True)
        (tmp_path / "ws" / "av" / "current").symlink_to(build_dir)

        with patch(f"{self._ARTIFACTS_DIR}.BUILD_DIR", str(tmp_path)):
            result = get_current_build_dir("ws", "av")
            assert result is not None
            assert result.is_dir()

    def test_returns_none_when_symlink_target_missing(self, tmp_path):
        link_parent = tmp_path / "ws" / "av"
        link_parent.mkdir(parents=True)
        (link_parent / "current").symlink_to(link_parent / "builds" / "999")

        with patch(f"{self._ARTIFACTS_DIR}.BUILD_DIR", str(tmp_path)):
            assert get_current_build_dir("ws", "av") is None


class TestExecuteBuild:
    @pytest.fixture(autouse=True)
    def _stub_manifests(self):
        # resolve_all() enumerates declared module config fields via get_manifests();
        # stub it so these tests don't read the real on-disk module config.json.
        with patch("agento.framework.bootstrap.get_manifests", return_value=[]):
            yield

    def _mock_conn(self, *, ws_code="testws", existing_build=None):
        """Create a mock DB connection with cursor context manager."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        call_count = 0
        def fetchone_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"code": ws_code}
            if call_count == 2:
                return existing_build
            return None

        cursor.fetchone.side_effect = fetchone_side_effect
        cursor.fetchall.return_value = []  # global-scope DB overrides for strategy
        cursor.lastrowid = 42
        return conn, cursor

    @patch("agento.framework.harness.workspace_adapter_for")
    @patch("agento.framework.agent_view_runtime.resolve_agent_view_runtime")
    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.framework.workspace.get_agent_view")
    def test_full_build_flow(self, mock_get_av, mock_overrides, mock_resolve, mock_get_writer, tmp_path):
        mock_get_av.return_value = _make_agent_view()
        mock_overrides.return_value = {"agent_view/harness": ("claude", False)}

        from agento.framework.agent_view_runtime import AgentViewRuntime
        mock_resolve.return_value = AgentViewRuntime(harness="claude", provider="anthropic")
        mock_writer = MagicMock()
        mock_get_writer.return_value = mock_writer

        conn, _ = self._mock_conn()

        with patch(f"{_BUILDER}.BUILD_DIR", str(tmp_path)):
            result = execute_build(conn, 1)

        assert isinstance(result, BuildResult)
        assert result.build_id == 42
        assert result.skipped is False
        assert len(result.checksum) == 64
        mock_get_writer.assert_called_once_with("claude")
        conn.commit.assert_called()

    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.framework.workspace.get_agent_view")
    def test_skips_existing_build(self, mock_get_av, mock_overrides, tmp_path):
        mock_get_av.return_value = _make_agent_view()
        mock_overrides.return_value = {"agent_view/harness": ("claude", False)}
        existing_dir = tmp_path / "existing_build"
        existing_dir.mkdir()
        existing = {"id": 99, "build_dir": str(existing_dir)}
        conn, _ = self._mock_conn(existing_build=existing)

        result = execute_build(conn, 1)
        assert result.skipped is True
        assert result.build_id == 99

    @patch("agento.framework.harness.workspace_adapter_for")
    @patch("agento.framework.agent_view_runtime.resolve_agent_view_runtime")
    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.framework.workspace.get_agent_view")
    def test_checksum_reacts_to_env_override(
        self, mock_get_av, mock_overrides, mock_resolve, mock_get_writer,
        tmp_path, monkeypatch,
    ):
        """An ENV override of an agent_view field must change the build checksum
        so the freshness check rebuilds (it previously hashed DB-only values)."""
        mock_get_av.return_value = _make_agent_view()
        mock_overrides.return_value = {"agent_view/harness": ("claude", False)}
        from agento.framework.agent_view_runtime import AgentViewRuntime
        mock_resolve.return_value = AgentViewRuntime(harness="claude", provider="anthropic")
        mock_get_writer.return_value = MagicMock()

        with patch(f"{_BUILDER}.BUILD_DIR", str(tmp_path)):
            conn1, _ = self._mock_conn()
            checksum_no_env = execute_build(conn1, 1).checksum

            monkeypatch.setenv("CONFIG__AGENT_VIEW__MODEL", "env-model")
            conn2, _ = self._mock_conn()
            checksum_with_env = execute_build(conn2, 1).checksum

        assert checksum_no_env != checksum_with_env

    @patch("agento.framework.harness.workspace_adapter_for")
    @patch("agento.framework.agent_view_runtime.resolve_agent_view_runtime")
    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.framework.workspace.get_agent_view")
    def test_writes_instruction_file_from_env(
        self, mock_get_av, mock_overrides, mock_resolve, mock_get_writer,
        tmp_path, monkeypatch,
    ):
        """AGENTS.md set only via ENV (no DB row) is materialized into the build."""
        mock_get_av.return_value = _make_agent_view()
        mock_overrides.return_value = {"agent_view/harness": ("claude", False)}
        from agento.framework.agent_view_runtime import AgentViewRuntime
        mock_resolve.return_value = AgentViewRuntime(harness="claude", provider="anthropic")
        mock_get_writer.return_value = MagicMock()
        monkeypatch.setenv("CONFIG__AGENT_VIEW__INSTRUCTIONS__AGENTS_MD", "# from env")

        with patch(f"{_BUILDER}.BUILD_DIR", str(tmp_path)):
            result = execute_build(self._mock_conn()[0], 1)

        assert (Path(result.build_dir) / "AGENTS.md").read_text() == "# from env"

    @patch("agento.framework.harness.workspace_adapter_for")
    @patch("agento.framework.agent_view_runtime.resolve_agent_view_runtime")
    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.framework.workspace.get_agent_view")
    def test_rebuilds_when_existing_build_dir_missing_on_disk(
        self, mock_get_av, mock_overrides, mock_resolve, mock_get_writer, tmp_path,
    ):
        """If DB says ready but build_dir was deleted manually, rebuild instead of lying."""
        mock_get_av.return_value = _make_agent_view()
        mock_overrides.return_value = {"agent_view/harness": ("claude", False)}
        from agento.framework.agent_view_runtime import AgentViewRuntime
        mock_resolve.return_value = AgentViewRuntime(harness="claude", provider="anthropic")
        mock_get_writer.return_value = MagicMock()

        missing_dir = tmp_path / "deleted_by_user"
        assert not missing_dir.exists()
        existing = {"id": 99, "build_dir": str(missing_dir)}
        conn, cursor = self._mock_conn(existing_build=existing)

        with patch(f"{_BUILDER}.BUILD_DIR", str(tmp_path)):
            result = execute_build(conn, 1)

        assert result.skipped is False
        assert result.build_id == 42
        # Stale DB record was invalidated to 'failed'
        update_calls = [
            c for c in cursor.execute.call_args_list
            if "UPDATE workspace_build SET status = 'failed'" in c.args[0]
            and c.args[1] == (99,)
        ]
        assert update_calls, "Expected stale build 99 to be marked failed"

    @patch("agento.framework.harness.workspace_adapter_for")
    @patch("agento.framework.agent_view_runtime.resolve_agent_view_runtime")
    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.framework.workspace.get_agent_view")
    def test_force_bypasses_skip_and_cleans_prior_build(
        self, mock_get_av, mock_overrides, mock_resolve, mock_get_writer, tmp_path,
    ):
        """force=True: rebuild even when checksum matches and prior dir is intact."""
        mock_get_av.return_value = _make_agent_view()
        mock_overrides.return_value = {"agent_view/harness": ("claude", False)}
        from agento.framework.agent_view_runtime import AgentViewRuntime
        mock_resolve.return_value = AgentViewRuntime(harness="claude", provider="anthropic")
        mock_get_writer.return_value = MagicMock()

        existing_dir = tmp_path / "prior_build"
        existing_dir.mkdir()
        (existing_dir / "sentinel.txt").write_text("should be wiped")
        existing = {"id": 99, "build_dir": str(existing_dir)}
        conn, cursor = self._mock_conn(existing_build=existing)

        with patch(f"{_BUILDER}.BUILD_DIR", str(tmp_path)):
            result = execute_build(conn, 1, force=True)

        assert result.skipped is False
        assert result.build_id == 42  # new lastrowid, not the stale 99
        # Prior on-disk dir was cleaned up before the rebuild.
        assert not existing_dir.exists()
        # Stale DB record was retired.
        update_calls = [
            c for c in cursor.execute.call_args_list
            if "UPDATE workspace_build SET status = 'failed'" in c.args[0]
            and c.args[1] == (99,)
        ]
        assert update_calls, "Expected prior build 99 to be marked failed under --force"

    @patch("agento.framework.workspace.get_agent_view")
    def test_raises_on_missing_agent_view(self, mock_get_av):
        mock_get_av.return_value = None
        with pytest.raises(ValueError, match="agent_view 999 not found"):
            execute_build(MagicMock(), 999)

    @patch("agento.framework.workspace.get_agent_view")
    def test_raises_on_missing_workspace(self, mock_get_av):
        mock_get_av.return_value = _make_agent_view()
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.return_value = None

        with pytest.raises(ValueError, match="workspace 10 not found"):
            execute_build(conn, 1)

    @patch("agento.modules.workspace_build.src.builder.materialize_agent_credentials")
    @patch("agento.framework.harness.workspace_adapter_for")
    @patch("agento.framework.agent_view_runtime.resolve_agent_view_runtime")
    @patch("agento.framework.scoped_config.build_scoped_overrides")
    @patch("agento.framework.workspace.get_agent_view")
    def test_migrates_legacy_workspace_codex_config_into_per_agent_build(
        self,
        mock_get_av,
        mock_overrides,
        mock_resolve,
        mock_get_writer,
        mock_materialize_credentials,
        tmp_path,
    ):
        from agento.framework.agent_view_runtime import AgentViewRuntime
        from agento.modules.codex.src.config import CodexWorkspaceAdapter

        mock_get_av.return_value = _make_agent_view()
        mock_overrides.return_value = {"agent_view/provider": ("codex", False)}
        mock_resolve.return_value = AgentViewRuntime(harness="codex", provider="openai")
        mock_get_writer.return_value = CodexWorkspaceAdapter()
        mock_materialize_credentials.return_value = None

        legacy_codex = tmp_path / ".codex"
        legacy_codex.mkdir(parents=True)
        # Legacy config carries a user-added custom MCP server beyond toolbox.
        # The toolbox entry itself is re-derived by the writer from core/toolbox/url.
        (legacy_codex / "config.toml").write_text(
            "\n[mcp_servers.custom_extra]\n"
            'type = "streamable_http"\n'
            'url = "http://legacy-extra:9999/mcp"\n'
        )

        conn, _ = self._mock_conn()

        with patch(f"{_BUILDER}.BUILD_DIR", str(tmp_path / "build")):
            result = execute_build(conn, 1)

        build_config = tmp_path / "build" / "testws" / "dev" / "builds" / str(result.build_id) / ".codex" / "config.toml"
        assert build_config.is_file()
        content = build_config.read_text()
        # Fresh toolbox entry auto-injected by writer
        assert "toolbox:3001/mcp" in content
        # Legacy-only entries preserved via migrate_legacy_workspace_config
        assert "legacy-extra:9999/mcp" in content


class TestValidateCode:
    """Tests for workspace/agent_view code validation."""

    def test_valid_codes(self):
        from agento.framework.workspace import validate_code
        for code in ("default", "agent01", "my_company_dev", "my_workspace", "a"):
            validate_code(code)  # should not raise

    def test_rejects_underscore_prefix(self):
        from agento.framework.workspace import validate_code
        with pytest.raises(ValueError, match="must match"):
            validate_code("_hidden")

    def test_rejects_dot_prefix(self):
        from agento.framework.workspace import validate_code
        with pytest.raises(ValueError, match="must match"):
            validate_code(".dotted")

    def test_rejects_uppercase(self):
        from agento.framework.workspace import validate_code
        with pytest.raises(ValueError, match="must match"):
            validate_code("MyWorkspace")

    def test_rejects_empty(self):
        from agento.framework.workspace import validate_code
        with pytest.raises(ValueError, match="must match"):
            validate_code("")

    def test_rejects_starts_with_digit(self):
        from agento.framework.workspace import validate_code
        with pytest.raises(ValueError, match="must match"):
            validate_code("1abc")
