"""The one parser of an apps-origin file path (PRD E6 §6.2).

The authorization decision and the file lookup must agree on which file a request names.
So web parses the raw URI once, decides on what it parsed, and hands the proxy the
canonical path to fetch; the artifacts server only checks that it got a canonical path and
never normalizes. Contract fixture: tests/fixtures/app_path_v1.json.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote_to_bytes

ARTIFACT_CODE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
VERSION_ID_RE = re.compile(r"v-[0-9]{8}-[0-9]{6}-[a-z0-9]{4}")
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class AppPath:
    artifact_code: str
    version_id: str
    upstream_path: str


def _segment(raw: str) -> str | None:
    """One decoded file-path segment, or None. Decoded exactly once, strictly."""
    if _BAD_ESCAPE.search(raw):
        return None
    try:
        seg = unquote_to_bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not seg or seg.startswith(".") or "/" in seg or "\\" in seg or _CONTROL.search(seg):
        return None
    return seg


def parse_app_path(raw_uri: object) -> AppPath | None:
    if not isinstance(raw_uri, str):
        return None
    raw = raw_uri.split("?", 1)[0]
    if not raw.startswith("/a/") or _CONTROL.search(raw):
        return None
    parts = raw[3:].split("/")
    if len(parts) < 4:
        return None
    code, v, version, *rest = parts
    if not ARTIFACT_CODE_RE.fullmatch(code) or v != "v" or not VERSION_ID_RE.fullmatch(version):
        return None
    trailing = rest[-1] == ""
    names = rest[:-1] if trailing else rest
    decoded = [_segment(s) for s in names]
    if any(s is None for s in decoded):
        return None
    tail = "/".join(quote(s, safe="") for s in decoded)
    upstream = f"/{code}/v/{version}/" + tail + ("/" if trailing and tail else "")
    return AppPath(code, version, upstream)
