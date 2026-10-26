"""One rule for every harness (E9 §3.1): the final answer is the text of the LAST assistant
message. Earlier assistant messages are progress notes; user and tool text never leaks in."""
from __future__ import annotations

import json

import pytest

from agento.modules.claude.src.output_parser import parse_claude_output
from agento.modules.codex.src.runner import CodexSubprocessRunner
from agento.modules.pi.src.output_parser import parse_stream


def _lines(*events: dict) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def _claude() -> str:
    return _lines(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "user text"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "second"}]}},
        {"type": "result", "is_error": False, "result": "second", "session_id": "s"},
    )


def _codex() -> str:
    return _lines(
        {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "first"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "second"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    )


def _pi() -> str:
    return _lines(
        {"type": "message_end", "message": {"role": "assistant",
                                            "content": [{"type": "text", "text": "first"}]}},
        {"type": "message_end", "message": {"role": "user",
                                            "content": [{"type": "text", "text": "user text"}]}},
        {"type": "message_end", "message": {"role": "assistant",
                                            "content": [{"type": "text", "text": "second"}]}},
    )


def _codex_answer(raw: str) -> str:
    runner = object.__new__(CodexSubprocessRunner)
    return CodexSubprocessRunner._parse_output(runner, raw).raw_output


@pytest.mark.parametrize(("raw", "answer"), [
    (_claude(), lambda raw: parse_claude_output(raw).raw_output),
    (_codex(), _codex_answer),
    (_pi(), lambda raw: parse_stream(raw).text),
], ids=["claude", "codex", "pi"])
def test_final_answer_is_the_last_assistant_message(raw, answer):
    assert answer(raw) == "second"
