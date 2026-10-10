"""The streaming response contract (PRD E3-E5 §7.1).

A handler returns `StreamingResponse` instead of `Response` and the listener writes each
frame as the generator yields it. The whole contract:

* **No `Content-Length`.** uvicorn sends HTTP/1.1 chunks (to HTTP/1.0: up to the close), so
  the body ends where the generator ends.
* **A disconnect ends the loop** after the `next()` in progress: the listener watches ASGI
  `http.disconnect`, because uvicorn drops a write to a closed socket silently.
* **The generator's `finally` runs exactly once.** `close()` releases the §7.3 stream slot on
  every exit - clean end, disconnect, a handler that raised. A leaked slot is leaked for ever.
* **The generator runs on the stream thread budget** (`server.MAX_STREAMS`), never the
  request pool, so open streams cannot stop other requests.
* **A streaming route is an ordinary route.** Auth, CSRF and the rate limiter run first.

Heartbeats are ordinary frames from the same generator: a second writer would need its own
lock, disconnect handling and share of the `finally`.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

SSE_HEADERS = (
    ("Content-Type", "text/event-stream"),
    # Nothing between here and the reader may buffer the stream into a single reply.
    ("X-Accel-Buffering", "no"),
)


@dataclass
class StreamingResponse:
    status: int
    frames: Iterator[bytes]
    headers: list[tuple[str, str]] = field(default_factory=list)


def sse(frames: Iterator[bytes], *, status: int = 200) -> StreamingResponse:
    return StreamingResponse(status, frames, headers=list(SSE_HEADERS))


def frame(*, data: str, event: str | None = None, event_id: int | str | None = None) -> bytes:
    """One `text/event-stream` frame.

    `id:` is what makes replay automatic: the browser resends the last one it saw as
    `Last-Event-ID` on reconnect (WHATWG HTML 9.2.3), so the cursor needs no client code.
    A newline inside `data` would end the frame early, so it is sent as one `data:` line
    per line - which is what the standard says a multi-line body is.
    """
    lines = []
    if event is not None:
        lines.append(f"event: {event}")
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.extend(f"data: {line}" for line in data.split("\n"))
    return ("\n".join(lines) + "\n\n").encode()
