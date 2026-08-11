from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from agento.framework.harness import ParseSummary, ToolUse
from agento.modules.codex.src import transcript_reader as cx_tr
from agento.modules.codex.src.transcript_reader import (
    CodexTranscriptReader,
    _find_transcript,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "transcripts" / "codex"

GOOD_ID = "11111111-1111-1111-1111-111111111111"
BAD_ID = "22222222-2222-2222-2222-222222222222"


def _layout(build: Path) -> None:
    """Lay fixtures out under
    ``build/<workspace>/<agent_view>/<build_id>/.codex/sessions/YYYY/MM/DD/rollout-...-<uuid>.jsonl``.
    """
    sessions = (
        build
        / "acme" / "developer" / "build-001"
        / ".codex" / "sessions" / "2026" / "05" / "14"
    )
    sessions.mkdir(parents=True)
    shutil.copy(
        FIXTURES / "codex_good_with_mcp.jsonl",
        sessions / f"rollout-2026-05-14T05-05-33-{GOOD_ID}.jsonl",
    )
    shutil.copy(
        FIXTURES / "codex_bad_no_mcp.jsonl",
        sessions / f"rollout-2026-05-14T05-10-00-{BAD_ID}.jsonl",
    )


@pytest.fixture
def build_root(tmp_path: Path, monkeypatch) -> Path:
    build = tmp_path / "build"
    _layout(build)
    monkeypatch.setattr(cx_tr, "BUILD_DIR", str(build))
    return build


def test_parse_picks_up_modern_mcp_call(build_root: Path):
    summary = CodexTranscriptReader().parse(GOOD_ID)
    names = [u.name for u in summary.tool_uses]
    # Modern: namespace + name
    assert "mcp__toolbox__jira_get_issue" in names


def test_parse_picks_up_event_correlated_mcp_call(build_root: Path):
    summary = CodexTranscriptReader().parse(GOOD_ID)
    names = [u.name for u in summary.tool_uses]
    # Event-correlated: server=toolbox, tool=list_resources → mcp__toolbox__list_resources
    assert "mcp__toolbox__list_resources" in names


def test_parse_picks_up_legacy_mcp_name(build_root: Path):
    summary = CodexTranscriptReader().parse(GOOD_ID)
    names = [u.name for u in summary.tool_uses]
    # Legacy: name already prefixed with mcp__, emit as-is
    assert "mcp__toolbox__legacy_tool" in names


def test_parse_emits_non_mcp_tools_too(build_root: Path):
    summary = CodexTranscriptReader().parse(GOOD_ID)
    names = [u.name for u in summary.tool_uses]
    assert "exec_command" in names


def test_parse_yields_call_ids_as_tool_use_id(build_root: Path):
    summary = CodexTranscriptReader().parse(GOOD_ID)
    assert all(isinstance(u, ToolUse) for u in summary.tool_uses)
    assert all(u.tool_use_id.startswith("call_") for u in summary.tool_uses)


def test_parse_no_mcp_returns_only_local_tools(build_root: Path):
    summary = CodexTranscriptReader().parse(BAD_ID)
    assert summary.tool_uses, "fixture should contain non-MCP tool uses"
    names = [u.name for u in summary.tool_uses]
    assert all(not n.startswith("mcp__") for n in names)
    assert "exec_command" in names
    assert "apply_patch" in names


def test_parse_skips_malformed_lines(build_root: Path):
    # codex_good_with_mcp.jsonl has an embedded plaintext line.
    summary = CodexTranscriptReader().parse(GOOD_ID)
    call_ids = sorted(u.tool_use_id for u in summary.tool_uses)
    assert call_ids == ["call_001", "call_002", "call_003", "call_004"]


def test_iter_tool_uses_returns_same_stream_as_parse(build_root: Path):
    reader = CodexTranscriptReader()
    summary = reader.parse(GOOD_ID)
    streamed = tuple(reader.iter_tool_uses(GOOD_ID))
    assert streamed == summary.tool_uses


def test_missing_transcript_raises(build_root: Path):
    with pytest.raises(FileNotFoundError):
        CodexTranscriptReader().parse("00000000-0000-0000-0000-000000000000")


def test_find_transcript_returns_path(build_root: Path):
    path = _find_transcript(GOOD_ID, build_root)
    assert GOOD_ID in path.name
    assert path.is_file()


def test_find_transcript_prefers_most_recent_when_duplicated(tmp_path: Path, monkeypatch):
    # Two rollout files for the same session UUID — most recent wins.
    sessions = (
        tmp_path / "build" / "acme" / "developer" / "build-001"
        / ".codex" / "sessions" / "2026" / "05" / "14"
    )
    sessions.mkdir(parents=True)
    older = sessions / f"rollout-2026-05-14T05-05-33-{GOOD_ID}.jsonl"
    newer = sessions / f"rollout-2026-05-14T06-00-00-{GOOD_ID}.jsonl"
    shutil.copy(FIXTURES / "codex_good_with_mcp.jsonl", older)
    shutil.copy(FIXTURES / "codex_good_with_mcp.jsonl", newer)
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_800_000_000, 1_800_000_000))
    monkeypatch.setattr(cx_tr, "BUILD_DIR", str(tmp_path / "build"))

    path = _find_transcript(GOOD_ID, tmp_path / "build")
    assert path == newer


def test_parse_summary_counts_recognized_records(build_root: Path):
    """Good fixture lines all carry a known ``type``. recognized_records must
    equal total_json_lines (no drift in this fixture)."""
    summary = CodexTranscriptReader().parse(GOOD_ID)
    assert summary.total_json_lines > 0
    assert summary.recognized_records == summary.total_json_lines


def test_parse_drift_unrecognized_format(tmp_path: Path, monkeypatch):
    """Hypothetical: codex ships a JSONL with a brand-new outer envelope.
    Reader must report total_json_lines>0 and recognized_records==0 so the
    observer can detect drift instead of treating it as "no MCP calls".
    """
    build = tmp_path / "build"
    sessions = (
        build / "acme" / "developer" / "build-001"
        / ".codex" / "sessions" / "2026" / "05" / "14"
    )
    sessions.mkdir(parents=True)
    sid = "33333333-3333-3333-3333-333333333333"
    body = "\n".join(
        f'{{"unexpected": "format", "v": {i}}}' for i in range(8)
    )
    (sessions / f"rollout-2026-05-14T07-00-00-{sid}.jsonl").write_text(body + "\n")

    monkeypatch.setattr(cx_tr, "BUILD_DIR", str(build))
    summary = CodexTranscriptReader().parse(sid)
    assert summary.total_json_lines == 8
    assert summary.recognized_records == 0
    assert summary.tool_uses == ()
    assert isinstance(summary, ParseSummary)


def test_satisfies_protocol():
    from agento.framework.harness import TranscriptReader
    assert isinstance(CodexTranscriptReader(), TranscriptReader)
