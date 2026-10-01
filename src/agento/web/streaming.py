"""The streaming response contract (PRD E3-E5 §7.1).

A handler returns `StreamingResponse` instead of `Response` and the listener writes each
frame as the generator yields it. Four properties, and they are the whole contract:

* **No `Content-Length`.** The length is not known when the headers go out, and a stream
  that had one would not be a stream. The connection is closed at the end instead, so
  the client sees the body end where the generator ends.
* **A disconnect is a broken write.** There is no out-of-band notification that a reader
  left; the write to a dead socket raises, and that raise is what ends the generator.
* **The generator's `finally` runs exactly once.** `close()` is what releases the §7.3
  stream slot, and the listener calls it on every exit - the clean end, the broken write,
  and a handler that raised mid-stream. A slot leaked here is a slot leaked for ever.
* **A streaming route is an ordinary route.** Auth, CSRF and the rate limiter run before
  the handler is called, exactly as for a materialized response; nothing about the return
  type changes the gate in front of it.

Heartbeats are ordinary frames from the same generator. A second writer would need its own
lock, its own disconnect handling and its own share of the `finally`; one generator has
none of those problems.
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
