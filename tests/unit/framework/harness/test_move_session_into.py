"""``move_session_into`` moves a session with its sibling dir of the same stem."""
from __future__ import annotations

from agento.framework.harness.subprocess_runner import move_session_into


def test_move_session_into_moves_sidecar_dir(tmp_path):
    (tmp_path / "old" / "s1" / "tool-results").mkdir(parents=True)
    (tmp_path / "old" / "s1.jsonl").write_text("s1")
    (tmp_path / "old" / "s1" / "tool-results" / "t.txt").write_text("t")

    assert move_session_into(tmp_path, "new", "s1.jsonl") is True
    assert (tmp_path / "new" / "s1.jsonl").read_text() == "s1"
    assert (tmp_path / "new" / "s1" / "tool-results" / "t.txt").read_text() == "t"
    assert list((tmp_path / "old").iterdir()) == []


def test_move_session_into_without_sidecar_unchanged(tmp_path):
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "s1.jsonl").write_text("s1")

    assert move_session_into(tmp_path, "new", "s1.jsonl") is True
    assert sorted(p.name for p in (tmp_path / "new").iterdir()) == ["s1.jsonl"]
