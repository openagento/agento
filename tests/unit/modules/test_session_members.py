"""The optional harness members ``session_file`` and ``session_transcript`` (plan §5.5)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agento.modules.claude.src.adapter import ClaudeHarnessAdapter
from agento.modules.codex.src.adapter import CodexHarnessAdapter
from agento.modules.pi.src.adapter import PiHarnessAdapter

SID = "00000000-0000-4000-8000-000000000001"
FIXTURE = Path(__file__).parents[2] / "fixtures" / "claude_session_two_turns.jsonl"

HARNESSES = [
    pytest.param(ClaudeHarnessAdapter, ".claude/projects/{bucket}/{sid}.jsonl", id="claude"),
    pytest.param(CodexHarnessAdapter,
                 ".codex/sessions/2026/01/{bucket}/rollout-2026-01-01T10-00-00-{sid}.jsonl", id="codex"),
    pytest.param(PiHarnessAdapter,
                 ".pi/agent/sessions/{bucket}/2026-01-01T10-00-00-000Z_{sid}.jsonl", id="pi"),
]


def _put(root: Path, template: str, bucket: str, mtime: int) -> Path:
    path = root / template.format(bucket=bucket, sid=SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    os.utime(path, (mtime, mtime))
    return path


@pytest.mark.parametrize(("adapter", "template"), HARNESSES)
def test_session_file_finds_the_newest_file_of_the_session(tmp_path, adapter, template):
    _put(tmp_path, template, "01", 1000)
    newest = _put(tmp_path, template, "02", 2000)
    assert adapter().session_file(tmp_path, SID) == newest


@pytest.mark.parametrize(("adapter", "template"), HARNESSES)
@pytest.mark.parametrize("session_id", [SID.replace("1", "2"), "../x", "*", ""])
def test_session_file_is_none_for_an_absent_or_bad_id(tmp_path, adapter, template, session_id):
    _put(tmp_path, template, "01", 1000)
    assert adapter().session_file(tmp_path, session_id) is None


def _texts(turn: list[dict]) -> list[tuple]:
    return [(f["kind"], f.get("text") or f.get("tool_name") or f["data"]["output"], f.get("at"))
            for f in turn]


def test_session_transcript_splits_turns_and_skips_noise():
    turns = ClaudeHarnessAdapter().session_transcript(FIXTURE)
    assert turns is not None
    assert [_texts(t) for t in turns] == [
        [("assistant.reasoning", "Think about the task.", "2026-01-01T10:02:00.000Z"),
         ("tool.started", "example_tool", "2026-01-01T10:03:00.000Z"),
         # A tool_result user line does not start a turn.
         ("tool.completed", "Tool output.", "2026-01-01T10:04:00.000Z"),
         ("assistant.text", "First answer, see https://example.com.", "2026-01-01T10:06:00.000Z")],
        # A line with no timestamp has no `at`.
        [("assistant.text", "Second answer.", None)],
    ]


def test_session_transcript_of_one_turn(tmp_path):
    one = tmp_path / "one.jsonl"
    one.write_text("".join(FIXTURE.read_text().splitlines(keepends=True)[:15]))
    turns = ClaudeHarnessAdapter().session_transcript(one)
    assert turns is not None and len(turns) == 1


def test_session_transcript_of_an_unreadable_file_is_none(tmp_path):
    assert ClaudeHarnessAdapter().session_transcript(tmp_path / "absent.jsonl") is None


def test_session_transcript_of_a_bad_file_is_none_or_a_fallback_shape(tmp_path):
    """A file that is not UTF-8 is None; a line of an odd shape is skipped, never a crash."""
    binary = tmp_path / "binary.jsonl"
    binary.write_bytes(b'{"type": "user"}\n\xff\xfe\n')
    assert ClaudeHarnessAdapter().session_transcript(binary) is None
    odd = tmp_path / "odd.jsonl"
    odd.write_text('{"type": "user", "message": "text"}\n')
    assert ClaudeHarnessAdapter().session_transcript(odd) == [[]]


_STREAM = ('{"type":"system","subtype":"init","session_id":"s"}\n'
           '{"type":"assistant","message":{"content":[{"type":"text","text":"Done."}]}}\n'
           '{"type":"result","subtype":"success","result":"Done.","session_id":"s"}\n')


@pytest.mark.parametrize(("output", "answer"), [
    pytest.param("Plain answer.", "Plain answer.", id="answer"),
    pytest.param(_STREAM, "Done.", id="raw-stream"),
    pytest.param(_STREAM.rsplit("\n", 2)[0] + "\n", None, id="stream-without-result"),
])
def test_stored_answer_of_a_claude_job_output(output, answer):
    """An older run kept the whole stream-json in `job.output`; its answer is the result."""
    assert ClaudeHarnessAdapter().stored_answer(output) == answer
