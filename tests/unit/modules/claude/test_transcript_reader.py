from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from agento.framework.harness import ParseSummary, ToolUse
from agento.modules.claude.src import transcript_reader as cl_tr
from agento.modules.claude.src.transcript_reader import (
    ClaudeTranscriptReader,
    _find_transcript,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "transcripts"


@pytest.fixture
def build_root(tmp_path: Path, monkeypatch) -> Path:
    """Build a production-shape tree:

        <tmp_path>/build/acme/developer/build-001/.claude/projects/-workspace-x/<sid>.jsonl

    Each fixture file becomes a synthetic session id (the fixture stem).
    """
    build = tmp_path / "build"
    projects = build / "acme" / "developer" / "build-001" / ".claude" / "projects" / "-workspace-x"
    projects.mkdir(parents=True)
    for src in FIXTURES.glob("*.jsonl"):
        shutil.copy(src, projects / f"{src.stem}.jsonl")
    monkeypatch.setattr(cl_tr, "BUILD_DIR", str(build))
    return build


def test_parse_finds_mcp_call(build_root: Path):
    summary = ClaudeTranscriptReader().parse("good_with_mcp")
    names = [u.name for u in summary.tool_uses]
    assert "mcp__toolbox__jira_get_issue" in names
    assert "Read" in names
    assert all(
        isinstance(u, ToolUse) and u.tool_use_id.startswith("toolu_")
        for u in summary.tool_uses
    )
    assert summary.recognized_records > 0
    assert summary.total_json_lines >= summary.recognized_records


def test_iter_tool_uses_returns_same_stream_as_parse(build_root: Path):
    reader = ClaudeTranscriptReader()
    summary = reader.parse("good_with_mcp")
    streamed = tuple(reader.iter_tool_uses("good_with_mcp"))
    assert streamed == summary.tool_uses


def test_parse_no_mcp_returns_only_builtins(build_root: Path):
    summary = ClaudeTranscriptReader().parse("bad_no_mcp")
    assert summary.tool_uses, "fixture should contain non-MCP tool uses"
    assert all(not u.name.startswith("mcp__toolbox__") for u in summary.tool_uses)
    assert summary.recognized_records > 0


def test_parse_text_only_yields_no_tool_uses(build_root: Path):
    summary = ClaudeTranscriptReader().parse("bad_text_only")
    assert summary.tool_uses == ()
    # Text-only assistant messages still match the message.content list shape,
    # so they count as recognized records — the absence of tool_use items
    # makes this a legit "agent did no work" case, not parser drift.
    assert summary.recognized_records > 0


def test_parse_skips_malformed_lines(build_root: Path):
    summary = ClaudeTranscriptReader().parse("mixed_other_mcp")
    names = [u.name for u in summary.tool_uses]
    assert "mcp__context7__resolve-library-id" in names
    assert all(not n.startswith("mcp__toolbox__") for n in names)


def test_missing_transcript_raises(build_root: Path):
    with pytest.raises(FileNotFoundError):
        ClaudeTranscriptReader().parse("does-not-exist")


def test_find_transcript_returns_path(build_root: Path):
    path = _find_transcript("good_with_mcp", build_root)
    assert path.name == "good_with_mcp.jsonl"
    assert path.is_file()


def test_recursive_glob_handles_state_symlink_layout(tmp_path: Path, monkeypatch):
    """Production layout: the build dir's ``.claude/projects`` is a symlink to
    ``state/.claude/projects`` (set up by workspace_build.link_persistent_paths).
    The recursive glob must follow the symlink chain and find the file.
    """
    if sys.platform == "win32":
        pytest.skip("symlink layout is POSIX-only in agento")

    build = tmp_path / "build"
    av_dir = build / "acme" / "developer"
    state_projects = av_dir / "state" / ".claude" / "projects" / "-workspace-x"
    state_projects.mkdir(parents=True)
    shutil.copy(FIXTURES / "good_with_mcp.jsonl", state_projects / "sess-xyz.jsonl")

    build_id_dir = av_dir / "build-001"
    (build_id_dir / ".claude").mkdir(parents=True)
    # Relative symlink mirrors workspace_build.link_persistent_paths semantics.
    target = Path("..") / ".." / "state" / ".claude" / "projects"
    os.symlink(target, build_id_dir / ".claude" / "projects")

    monkeypatch.setattr(cl_tr, "BUILD_DIR", str(build))
    summary = ClaudeTranscriptReader().parse("sess-xyz")
    assert any(u.name.startswith("mcp__toolbox__") for u in summary.tool_uses)


def test_parse_drift_unrecognized_format_yields_no_recognized_records(tmp_path: Path, monkeypatch):
    """Hypothetical: claude-cli ships a new JSONL shape with no ``message.content``
    list. Reader must report total_json_lines>0 and recognized_records==0 so the
    observer can detect drift instead of mistaking it for "no MCP calls".
    """
    build = tmp_path / "build"
    proj = build / "acme" / "developer" / "build-001" / ".claude" / "projects" / "-workspace-x"
    proj.mkdir(parents=True)
    sid = "drift-sid"
    body = "\n".join(
        f'{{"unexpected": "format", "v": {i}}}' for i in range(8)
    )
    (proj / f"{sid}.jsonl").write_text(body + "\n")

    monkeypatch.setattr(cl_tr, "BUILD_DIR", str(build))
    summary = ClaudeTranscriptReader().parse(sid)
    assert summary.total_json_lines == 8
    assert summary.recognized_records == 0
    assert summary.tool_uses == ()
    assert isinstance(summary, ParseSummary)


def test_satisfies_protocol():
    from agento.framework.harness import TranscriptReader
    assert isinstance(ClaudeTranscriptReader(), TranscriptReader)
