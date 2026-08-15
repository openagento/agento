"""Tests for stream_style — styling + the terminal-escape sanitizer.

`sanitize` is the single choke point that keeps untrusted event text (model
output, tool results, content from external systems) from carrying live
terminal control sequences into the operator's terminal under
`agento run --pretty`. The raw JSONL path escapes them; rendering decodes them.
"""
from __future__ import annotations

import pytest

from agento.framework.harness.stream_style import bold, dim, sanitize, truncate

OSC_52_CLIPBOARD = "safe\x1b]52;c;YXR0YWNr\x07"
CSI_CLEAR_SCREEN = "safe\x1b[2J\x1b[H"


class TestSanitize:
    @pytest.mark.parametrize(
        "payload",
        [
            OSC_52_CLIPBOARD,
            CSI_CLEAR_SCREEN,
            "safe\x1b]0;retitled\x1b\\",       # OSC terminated by ST
            "safe\x1b[38;5;196mred",            # SGR colour injection
            "safe\x1b7\x1b8",                   # save/restore cursor
            "safe\x08\x08\x08overwrite",        # backspace rewriting
            "safe\x9b2J",                       # C1 CSI (8-bit form)
            "safe\x00\x07bell",                 # NUL + BEL
        ],
    )
    def test_no_control_character_survives(self, payload):
        out = sanitize(payload)
        assert "safe" in out
        assert "\x1b" not in out
        assert not any(ord(ch) < 0x20 and ch not in "\n\t" for ch in out)
        assert not any(0x7F <= ord(ch) <= 0x9F for ch in out)

    def test_osc_52_payload_is_removed_entirely(self):
        # Not merely the ESC: the whole sequence must go, or the payload text
        # would be printed and the reader could not tell it was an attack.
        assert sanitize(OSC_52_CLIPBOARD) == "safe"

    def test_newlines_and_tabs_survive(self):
        assert sanitize("a\nb\tc") == "a\nb\tc"

    def test_ordinary_text_is_untouched(self):
        text = "Będę czytał LESSONS.md — 100% ✓"
        assert sanitize(text) == text

    def test_recurses_through_a_whole_event(self):
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": OSC_52_CLIPBOARD},
                    {"type": "tool_use", "name": CSI_CLEAR_SCREEN, "input": {}},
                ]
            },
        }
        out = sanitize(event)
        blocks = out["message"]["content"]
        assert blocks[0]["text"] == "safe"
        assert blocks[1]["name"] == "safe"

    def test_non_string_leaves_are_preserved(self):
        event = {"num_turns": 3, "cost": 0.5, "ok": True, "none": None}
        assert sanitize(event) == event

    def test_dict_keys_are_sanitized_too(self):
        assert sanitize({OSC_52_CLIPBOARD: "v"}) == {"safe": "v"}


class TestStyling:
    def test_styling_is_plain_text_when_stdout_is_not_a_tty(self):
        # pytest captures stdout, so isatty() is False here.
        assert dim("x") == "x"
        assert bold("x") == "x"

    def test_truncate_collapses_whitespace_and_cuts_to_limit(self):
        assert truncate("a  b\tc") == "a b c"
        out = truncate("x" * 50, 10)
        assert len(out) == 10
        assert out.endswith("…")

    def test_truncate_leaves_short_text_alone(self):
        assert truncate("short", 10) == "short"

    def test_truncate_keeps_the_first_line_only_and_counts_the_rest(self):
        assert truncate("first\nsecret\nmore") == "first (+2 lines)"
        assert truncate("first\nsecond") == "first (+1 line)"

    def test_truncate_counts_dropped_lines_after_cutting_the_first(self):
        out = truncate("x" * 50 + "\ny", 10)
        assert out == "x" * 9 + "… (+1 line)"

    def test_truncate_ignores_surrounding_blank_lines(self):
        assert truncate("\n  only  \n\n") == "only"
