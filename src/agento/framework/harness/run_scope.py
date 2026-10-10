"""The query that names one run to the toolbox.

The toolbox derives the run's desk from what the URL names, so the URL has to name
something UNIQUE per run: a job supplies its numeric id, and an interactive ``agento
run`` supplies its string run id — which is already the last segment of the artifacts
dir, so both name the same directory the run works in. A URL that names neither lands
on the shared ``_fallback`` desk, where two runs would write the same files, so it is
better to name nothing at all and let the desk tools refuse.

``_fallback`` is therefore rejected as a run id here too: it passes the character class
but means "unknown session", which is the one thing a run id must never be.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# The same character class the toolbox accepts for a path segment. No dot, so `..`
# cannot appear; no separator, so one segment stays one segment.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_FALLBACK = "_fallback"


def run_scope_query(job_id: int | None, run_id: str | None) -> str:
    """Return ``job_id=…`` / ``run_id=…`` for a toolbox URL, or ``""`` for no scope."""
    if job_id is not None:
        return f"job_id={job_id}"
    if isinstance(run_id, str) and run_id != _FALLBACK and _RUN_ID_RE.match(run_id):
        return f"run_id={run_id}"
    return ""


def scope_toolbox_url(url: str, job_id: int | None, run_id: str | None) -> str:
    """Append the run scope to a toolbox URL, leaving an unscopeable run unchanged."""
    query = run_scope_query(job_id, run_id)
    if not query or not isinstance(url, str) or not url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{query}"


def toolbox_auth(url: str, token: str) -> tuple[str, dict[str, str]]:
    """Attach the run's capability to a toolbox MCP URL: ``(url, headers)``.

    ``/mcp`` gets ``Authorization: Bearer`` and an unchanged URL — a query token lands in
    access logs. ``/sse`` keeps ``?cap=``: an SSE client posts to the endpoint the server
    advertises verbatim and sends no headers. Call it only for a URL that
    ``is_toolbox_endpoint`` accepted, so the credential never reaches a third party.
    """
    try:
        path = urlsplit(url).path.rstrip("/")
    except ValueError:
        path = ""
    if path.endswith("/sse"):
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}cap={token}", {}
    return url, {"Authorization": f"Bearer {token}"}
