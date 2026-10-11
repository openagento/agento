"""The run worker: one process per ``execute``, started by the root runner server as the
run's own uid (``uids.py``). ``python -m agento.framework.runner.worker``.

It reads one ``execute`` request on stdin (the server closes it: secrets cross in memory,
never in argv or a file), loads the harness registry (no observer, no event: EVT-3), runs
the harness, and writes each event to stdout in the ``wire`` format. The server relays
them; it holds the terminal ``result``/``error`` frame until its cleanup is done.
"""
from __future__ import annotations

import inspect
import json
import logging
import signal
import subprocess
import sys
from dataclasses import asdict
from functools import partial

from ..harness.runtime import RunRequest
from ..retry_policy import NON_RETRYABLE_ERRORS
from . import wire

log = logging.getLogger("agento.runner")
# Raised to the consumer by name; anything else arrives as a RuntimeError. Every name the
# retry policy reads must keep its name, or a terminal error is retried.
ERRORS = NON_RETRYABLE_ERRORS | {"TimeoutExpired", "RuntimeError"}
USAGE = ("input_tokens", "output_tokens", "duration_ms", "model")


def serve(req: dict, send, make_runner) -> None:
    """Run one ``execute`` request; ``send(event)`` writes one frame. The terminal event
    is sent last, its long text fields first as ``output`` chunks (``finish``)."""
    ctx = wire.context_from_wire(req["context"])
    try:  # a runner that cannot start is an error event too, never a lost connection
        runner = make_runner(ctx.harness, ctx, dry_run=bool(req.get("dry_run")))
        runner.observe(**_accepted(runner.observe, {
            "on_pid": lambda pid: send({"ev": "pid", "pid": pid}),
            "on_session_id": lambda sid: send({"ev": "session", "id": sid}),
            "on_line": _mapper(ctx.harness, send) if req.get("stream") else None,
            "on_usage": lambda r: send({"ev": "usage", **{k: getattr(r, k) for k in USAGE}})}))
        event = {"ev": "result", **asdict(runner.execute(RunRequest(**req["request"])))}
    except Exception as exc:
        event = _error_event(exc)
    finish(send, event)


def finish(send, event: dict) -> None:
    """Long text fields go first as ordered ``output`` chunks; no frame exceeds MAX_FRAME."""
    for key, value in list(event.items()):
        if isinstance(value, str) and len(value) > wire.CHUNK_CHARS:
            for i in range(0, len(value), wire.CHUNK_CHARS):
                send({"ev": "output", "field": key, "data": value[i:i + wire.CHUNK_CHARS]})
            del event[key]
    send(event)


def cut(event: dict) -> dict:
    """A fragment larger than one frame: its text is cut and it says so."""
    f = event["f"]
    text = f.get("text")
    return {"ev": "fragment", "f": {"kind": f.get("kind"), "tool_name": f.get("tool_name"),
                                    "text": text[:wire.CHUNK_CHARS] if isinstance(text, str) else None,
                                    "truncated": True}}


def _mapper(harness: str, send):
    """The map half of the delta seam; the consumer keeps the submit half (seq, redaction)."""
    from ..harness.registry import get_harness

    mapper = getattr(get_harness(harness).adapter, "stream_event_mapper", None)
    if mapper is None:
        return None
    return lambda line: [send({"ev": "fragment", "f": f}) for f in map_line(mapper, line)]


def map_line(mapper, line: str) -> list[dict]:
    """The fragments ``mapper`` makes of one stdout line. A line that is not a JSON object,
    or a mapper that raises, gives none: it costs a delta, never the run."""
    try:
        event = json.loads(line)
        mapped = mapper.map_event(event) if isinstance(event, dict) else None
    except Exception:
        return []
    return [f for f in (mapped if isinstance(mapped, list) else [mapped] if mapped else [])
            if isinstance(f, dict)]


def _accepted(method, callbacks: dict) -> dict:
    """The set callbacks ``method`` takes, by a signature check (CODE-3): the published
    ``Runner.observe`` has only on_pid/on_session_id, and an unknown keyword is a TypeError
    in someone else's runner (CODE-5)."""
    params = inspect.signature(method).parameters
    takes_all = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    return {k: v for k, v in callbacks.items() if v is not None and (takes_all or k in params)}


def _error_event(exc: Exception) -> dict:
    name = type(exc).__name__
    event = {"ev": "error", "type": name if name in ERRORS else "RuntimeError", "message": str(exc),
             "session_id": getattr(exc, "session_id", None),
             "agent_output": getattr(exc, "agent_output", None)}
    if isinstance(exc, subprocess.TimeoutExpired):
        event.update(message=str(exc.cmd), timeout=exc.timeout)
    if name == "UsageLimitError":
        event["reset_at"] = exc.reset_at
    return event


def stdout_sender(out=None):
    """``send`` for the server's pipe: one ``wire`` line per event, a too-large fragment cut."""
    out = out or sys.stdout.buffer

    def send(event: dict) -> None:
        line = wire.encode(event)
        if len(line) > wire.MAX_FRAME and event.get("ev") == "fragment":
            line = wire.encode(cut(event))
        out.write(line)
        out.flush()

    return send


def main() -> None:
    from .. import db
    from ..harness.loader import harness_reloader
    from ..harness.registry import create_runner
    from ..process_hardening import make_non_dumpable

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    make_non_dumpable()  # it holds this run's credential
    db.DISABLED = True  # no database here: a module that tries one fails fast
    # A stop (``signal`` op) reaches every process of the uid. The CLI stops; this worker
    # stays to report it, as before. A handler, not SIG_IGN: exec resets it for the CLI.
    signal.signal(signal.SIGTERM, lambda *_: None)
    line = sys.stdin.buffer.read(wire.MAX_REQUEST + 1)
    if len(line) > wire.MAX_REQUEST:
        sys.exit("worker: request too large")
    harness_reloader()()
    serve(json.loads(line), stdout_sender(), partial(create_runner, logger=log))


if __name__ == "__main__":
    main()
