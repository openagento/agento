"""Terminal styling for ``agento run --pretty`` stream renderers.

Framework code, so harness modules may import it without a ``sequence`` entry.
Color follows the same rule as the CLI's ``_output`` — on only when stdout is a
tty — so redirecting to a file yields clean text. The rule is restated here
rather than imported: ``framework/harness`` must not depend on ``framework/cli``
(pinned by ``test_review_round10_regressions.py``).
"""
from __future__ import annotations

import re
import sys

_DIM = "\033[2m"
_BOLD = "\033[1m"
_NC = "\033[0m"

BULLET = "⏺"  # tool call marker, as the agent CLIs draw it
BRANCH = "⎿"  # result continuation marker


# An escape sequence in event text: OSC (terminated by BEL or ST), CSI, or any
# other two-character ESC form. Stripped before anything reaches the terminal.
_ESCAPE_SEQUENCE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC ... BEL | ESC \
    r"|\x1b\[[0-?]*[ -/]*[@-~]"            # CSI ... final byte
    r"|\x1b[@-_]"                          # other escapes (incl. lone ESC forms)
)
# Control characters with no place in rendered text. Newline and tab survive.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _supports_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def sanitize(value):
    """Strip terminal control sequences from every string inside ``value``.

    Event text is untrusted: it carries model output, tool results, and content
    from external systems. The raw JSONL stream escapes control characters, so
    printing it is inert — rendering decodes them, which would make an OSC 52
    clipboard write or a cursor-manipulating CSI payload *active* in the
    operator's terminal. Applied once at the boundary, before any renderer sees
    the event, so no renderer can forget it.

    Recurses through dicts and lists; non-string leaves are returned unchanged.
    """
    if isinstance(value, str):
        return _CONTROL_CHARS.sub("", _ESCAPE_SEQUENCE.sub("", value))
    if isinstance(value, dict):
        # Keys too: a renderer may show a whole dict (Codex prints an MCP call's
        # `arguments`), and those keys can be attacker-controlled.
        return {sanitize(key): sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def dim(msg: str) -> str:
    return f"{_DIM}{msg}{_NC}" if _supports_color() else msg


def bold(msg: str) -> str:
    return f"{_BOLD}{msg}{_NC}" if _supports_color() else msg


def truncate(text: str, limit: int = 120) -> str:
    """First line of ``text``, collapsed and cut to ``limit`` characters.

    The dropped lines are counted, not hidden: ``"first\\nsecond\\nthird"``
    renders as ``first (+2 lines)``. One event is one line, so the rest cannot
    be shown — but the reader must be able to see that there was more.
    """
    lines = str(text).strip().splitlines()
    flat = " ".join(lines[0].split()) if lines else ""
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    dropped = len(lines) - 1
    if dropped > 0:
        more = f"(+{dropped} line{'' if dropped == 1 else 's'})"
        return f"{flat} {more}" if flat else more
    return flat
