"""Persistent HOME: a run gets only the session it resumes, and copy-back saves what it
changed to the session's own ``state/`` path — atomically, and never through a link."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from agento.framework import persistent_home
from agento.framework.persistent_home import (
    copy_back_persistent_paths,
    copy_in_persistent_paths,
    drop_persistent_paths,
    ensure_state_dir,
    session_relocations,
)

PATHS = [".claude/projects", ".claude/todos"]
OLD, NEW = 1_000_000_000, 2_000_000_000


def _write(path: Path, text: str, mtime: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def _finder(root: Path, sid: str) -> Path | None:
    return max(root.glob(f".claude/projects/*/{sid}.jsonl"), key=lambda p: p.stat().st_mtime,
               default=None)


@pytest.fixture
def state(tmp_path):
    state = tmp_path / "state"
    projects = state / ".claude" / "projects"
    _write(projects / "a" / "s1.jsonl", "s1")
    _write(projects / "a" / "s1" / "subagents" / "x.jsonl", "x")
    _write(projects / "a" / "s2.jsonl", "s2")
    _write(projects / "b" / "s3.jsonl", "s3")
    return state


class TestCopyIn:
    def test_copy_in_copies_only_given_session_and_sidecar(self, tmp_path, state):
        home = tmp_path / "home"
        copy_in_persistent_paths(home, state, PATHS,
                                 sessions=[state / ".claude/projects/a/s1.jsonl"])
        assert _files(home / ".claude/projects") == {"a/s1.jsonl", "a/s1/subagents/x.jsonl"}

    def test_copy_in_no_session_creates_nothing(self, tmp_path, state):
        home = tmp_path / "home"
        copy_in_persistent_paths(home, state, PATHS, sessions=())
        assert not (home / ".claude/projects").exists()
        assert not (home / ".claude/todos").exists()

    @pytest.mark.parametrize("kind", ["file", "empty dir", "absent"])
    def test_copy_in_file_path_never_created(self, tmp_path, kind):
        state, home = tmp_path / "state", tmp_path / "home"
        history = state / ".codex" / "history.jsonl"
        if kind == "file":
            _write(history, "h")
        elif kind == "empty dir":
            history.mkdir(parents=True)
        copy_in_persistent_paths(home, state, [".codex/history.jsonl", ".codex/sessions"], sessions=())
        assert not (home / ".codex" / "history.jsonl").exists()

    def test_copy_in_replaces_existing_symlink(self, tmp_path, state):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "projects").symlink_to(state / ".claude" / "projects")
        before = _files(state)
        copy_in_persistent_paths(home, state, PATHS, sessions=())
        assert not (home / ".claude" / "projects").is_symlink()
        assert _files(state) == before

    @pytest.mark.parametrize("session", [
        "outside",           # not under state/
        "persistent root",   # the persistent path itself, not a file under it
    ])
    def test_copy_in_ignores_session_outside_state(self, tmp_path, state, session):
        home = tmp_path / "home"
        path = (_write(tmp_path / "elsewhere" / "s9.jsonl", "x") if session == "outside"
                else state / ".claude" / "projects")
        copy_in_persistent_paths(home, state, PATHS, sessions=[path])
        assert _files(home) == set()

    def test_copy_in_ignores_session_symlink(self, tmp_path, state):
        home = tmp_path / "home"
        secret = _write(tmp_path / "secret", "secret")
        link = state / ".claude" / "projects" / "a" / "s9.jsonl"
        link.symlink_to(secret)
        copy_in_persistent_paths(home, state, PATHS, sessions=[link])
        assert _files(home) == set()

    def test_copy_in_never_writes_a_session_through_a_linked_parent(self, tmp_path, state):
        """A HOME whose `.claude` is a link (an old build asset): the session is not copied,
        and nothing lands outside the HOME (SEC-5)."""
        home, outside = tmp_path / "home", tmp_path / "outside"
        outside.mkdir()
        home.mkdir()
        (home / ".claude").symlink_to(outside)
        copy_in_persistent_paths(home, state, PATHS,
                                 sessions=[state / ".claude/projects/a/s1.jsonl"])
        assert _files(outside) == set()

    def test_copy_in_without_finder_copies_all(self, tmp_path, state):
        home = tmp_path / "home"
        copy_in_persistent_paths(home, state, PATHS, sessions=None)
        assert _files(home / ".claude/projects") == _files(state / ".claude/projects")


def test_ensure_state_dir_keeps_existing_file(tmp_path):
    _write(tmp_path / "ws" / "av" / "state" / ".codex" / "history.jsonl", "h")
    state = ensure_state_dir("ws", "av", [".codex/history.jsonl", ".codex/sessions"],
                             build_root=tmp_path)
    assert (state / ".codex" / "history.jsonl").read_text() == "h"
    assert (state / ".codex" / "sessions").is_dir()


class TestCopyBack:
    def _moved(self, tmp_path):
        state, home = tmp_path / "state", tmp_path / "home"
        _write(state / ".claude/projects/old/s1.jsonl", "turn1", OLD)
        _write(home / ".claude/projects/new/s1.jsonl", "turn1+turn2", NEW)
        return state, home

    def test_copy_back_relocates_moved_session(self, tmp_path):
        state, home = self._moved(tmp_path)
        relocate = {Path(".claude/projects/new/s1.jsonl"): Path(".claude/projects/old/s1.jsonl")}
        assert copy_back_persistent_paths(home, state, PATHS, relocate=relocate) == []
        assert (state / ".claude/projects/old/s1.jsonl").read_text() == "turn1+turn2"
        assert not (state / ".claude/projects/new").exists()

    def test_copy_back_relocates_sidecar_dir(self, tmp_path):
        state, home = self._moved(tmp_path)
        _write(home / ".claude/projects/new/s1/subagents/a.jsonl", "a", NEW)
        relocate = {Path(".claude/projects/new/s1.jsonl"): Path(".claude/projects/old/s1.jsonl")}
        copy_back_persistent_paths(home, state, PATHS, relocate=relocate)
        assert (state / ".claude/projects/old/s1/subagents/a.jsonl").read_text() == "a"
        assert not (state / ".claude/projects/new").exists()

    def test_copy_back_new_session_lands_at_its_own_path(self, tmp_path):
        state, home = tmp_path / "state", tmp_path / "home"
        _write(home / ".claude/projects/new/s7.jsonl", "s7")
        copy_back_persistent_paths(home, state, PATHS)
        assert (state / ".claude/projects/new/s7.jsonl").read_text() == "s7"

    def test_session_relocations_uses_finder_on_both_roots(self, tmp_path):
        state, home = self._moved(tmp_path)
        _write(home / ".claude/projects/same/s2.jsonl", "s2")
        _write(state / ".claude/projects/same/s2.jsonl", "s2")
        assert session_relocations(_finder, home, state, ["s1", "s2", "s3", None]) == {
            Path(".claude/projects/new/s1.jsonl"): Path(".claude/projects/old/s1.jsonl"),
        }

    def test_session_relocations_empty_without_finder(self, tmp_path):
        state, home = self._moved(tmp_path)
        assert session_relocations(None, home, state, ["s1"]) == {}

    @pytest.mark.parametrize("shape", ["session file", "sidecar dir", "parent dir"])
    def test_copy_back_never_reads_through_links(self, tmp_path, shape):
        state, home, outside = tmp_path / "state", tmp_path / "home", tmp_path / "outside"
        _write(outside / "projects" / "x" / "s1.jsonl", "secret")
        _write(outside / "projects" / "x" / "s1" / "t.txt", "secret")
        projects = home / ".claude" / "projects"
        if shape == "session file":
            projects.joinpath("x").mkdir(parents=True)
            projects.joinpath("x", "s1.jsonl").symlink_to(outside / "projects/x/s1.jsonl")
        elif shape == "sidecar dir":
            _write(projects / "x" / "s1.jsonl", "own")
            projects.joinpath("x", "s1").symlink_to(outside / "projects/x/s1")
        else:
            home.mkdir()
            (home / ".claude").symlink_to(outside)
        relocate = {Path(".claude/projects/x/s1.jsonl"): Path(".claude/projects/y/s1.jsonl")}
        copy_back_persistent_paths(home, state, PATHS, relocate=relocate)
        assert "secret" not in {p.read_text() for p in state.rglob("*") if p.is_file()}

    def test_session_relocations_skip_a_linked_session(self, tmp_path):
        state, home = tmp_path / "state", tmp_path / "home"
        _write(state / ".claude/projects/old/s1.jsonl", "s1")
        (home / ".claude/projects/new").mkdir(parents=True)
        (home / ".claude/projects/new/s1.jsonl").symlink_to(tmp_path / "x")
        _write(tmp_path / "x", "secret")
        assert session_relocations(_finder, home, state, ["s1"]) == {}

    def test_copy_back_codex_history_over_placeholder_dir(self, tmp_path):
        state, home = tmp_path / "state", tmp_path / "home"
        (state / ".codex" / "history.jsonl").mkdir(parents=True)
        _write(home / ".codex" / "history.jsonl", "h")
        assert copy_back_persistent_paths(home, state, [".codex/history.jsonl"]) == []
        assert (state / ".codex" / "history.jsonl").read_text() == "h"

    def test_known_names_same_path_with_other_bytes_is_a_conflict(self, tmp_path):
        """No session id known: a same-path state file with other bytes is never skipped as
        older, even when state/ is newer — the run's bytes would be lost."""
        state, home = tmp_path / "state", tmp_path / "home"
        _write(state / ".claude/projects/a/s1.jsonl", "state", NEW)
        src = _write(home / ".claude/projects/a/s1.jsonl", "run", OLD)
        index = persistent_home.state_name_index(state, PATHS)
        assert copy_back_persistent_paths(home, state, PATHS, known_names=index) == [src]
        assert (state / ".claude/projects/a/s1.jsonl").read_text() == "state"

    def test_known_names_one_home_with_two_copies_saves_one(self, tmp_path):
        """Two files of one name in one HOME: the first is saved and indexed, the second is
        an exact copy of it and is skipped."""
        state, home = tmp_path / "state", tmp_path / "home"
        state.mkdir()
        _write(home / ".claude/projects/a/s1.jsonl", "same")
        _write(home / ".claude/projects/b/s1.jsonl", "same")
        index = persistent_home.state_name_index(state, PATHS)
        assert copy_back_persistent_paths(home, state, PATHS, known_names=index) == []
        assert len(list(state.rglob("s1.jsonl"))) == 1
        assert len(index["s1.jsonl"]) == 1

    def test_copy_back_file_over_nonempty_dir_returns_conflict(self, tmp_path):
        state, home = tmp_path / "state", tmp_path / "home"
        _write(state / ".codex" / "history.jsonl" / "odd", "odd")
        _write(home / ".codex" / "history.jsonl", "h")
        _write(home / ".codex" / "sessions" / "s.jsonl", "s")
        conflicts = copy_back_persistent_paths(home, state, [".codex/history.jsonl", ".codex/sessions"])
        assert conflicts == [home / ".codex" / "history.jsonl"]
        assert (state / ".codex" / "sessions" / "s.jsonl").read_text() == "s"

    def test_copy_if_newer_failure_leaves_no_partial_file(self, tmp_path, monkeypatch):
        state, home = tmp_path / "state", tmp_path / "home"
        _write(state / ".claude/projects/a/s1.jsonl", "old", OLD)
        _write(home / ".claude/projects/a/s1.jsonl", "complete new content", NEW)
        real_copy2 = shutil.copy2

        def half_then_fail(src, dst, **kw):
            Path(dst).write_text("complete")
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(persistent_home.shutil, "copy2", half_then_fail)
        with pytest.raises(OSError):
            copy_back_persistent_paths(home, state, PATHS)
        assert (state / ".claude/projects/a/s1.jsonl").read_text() == "old"
        assert not list(state.rglob("*.tmp"))

        monkeypatch.setattr(persistent_home.shutil, "copy2", real_copy2)
        assert copy_back_persistent_paths(home, state, PATHS) == []
        assert (state / ".claude/projects/a/s1.jsonl").read_text() == "complete new content"


def test_drop_persistent_paths_unlinks_symlink_keeps_target(tmp_path):
    state, home = tmp_path / "state", tmp_path / "home"
    _write(state / ".claude/projects/a/s1.jsonl", "s1")
    _write(home / ".claude/todos/t.json", "t")
    (home / ".claude/projects").symlink_to(state / ".claude/projects")
    drop_persistent_paths(home, PATHS)
    assert not os.path.lexists(home / ".claude/projects")
    assert not (home / ".claude/todos").exists()
    assert (state / ".claude/projects/a/s1.jsonl").read_text() == "s1"


def test_drop_persistent_paths_leaves_a_path_under_a_linked_parent(tmp_path):
    state, home = tmp_path / "state", tmp_path / "home"
    _write(state / ".claude/projects/a/s1.jsonl", "s1")
    home.mkdir()
    (home / ".claude").symlink_to(state / ".claude")
    drop_persistent_paths(home, PATHS)
    assert (state / ".claude/projects/a/s1.jsonl").read_text() == "s1"
