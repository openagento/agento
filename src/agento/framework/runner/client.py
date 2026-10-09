"""The worker's side of the runner socket (``server.py``): ``RemoteRunner`` is a
``Runner``; ``run``/``pty`` start other vendor CLIs; ``alive``/``signal`` follow the owner
rule (docs/architecture/runner.md)."""
from __future__ import annotations

import builtins
import contextlib
import itertools
import json
import logging
import os
import re
import socket
import subprocess
from collections import deque
from datetime import datetime
from pathlib import Path

from . import wire

_RUNNER_NAME = re.compile(r"runner-[0-9]{1,4}\.sock")
_next = itertools.count()


def socket_path(name: str) -> str:
    """``runner-<i>.sock`` is in its own volume, mounted at ``runner-<i>/`` (SEC-7)."""
    root = os.environ.get("AGENTO_RUNNER_SOCKET_DIR", "/run/agento-runner")
    return os.path.join(root, name.removesuffix(".sock"), name)


def _pick() -> str:
    """The socket for a new child: round robin over ``runner-1..AGENTO_RUNNER_COUNT``."""
    count = max(1, int(os.environ.get("AGENTO_RUNNER_COUNT", "1")))
    return f"runner-{next(_next) % count + 1}.sock"


def _connect(name: str, timeout: float | None) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path(name))
    except OSError:
        sock.close()
        raise
    return sock


def _call(name: str, request: dict, timeout: float | None):
    """Send one request; yield ``(kind, event)``, with ``output`` chunks joined into the
    next event (the last ``MAX_OUTPUT`` characters of each field)."""
    with _connect(name, timeout) as sock:
        sock.sendall(wire.encode(request))
        parts: dict[str, deque] = {}
        reader = sock.makefile("rb")
        while line := reader.readline(wire.MAX_FRAME + 1):
            if not line.endswith(b"\n"):
                raise ValueError("runner frame too large")
            event = json.loads(line)
            kind = event.pop("ev")
            if kind == "output":
                # The server's _finish cuts at CHUNK_CHARS: this keeps the last MAX_OUTPUT chars.
                parts.setdefault(event["field"], deque(
                    maxlen=wire.MAX_OUTPUT // wire.CHUNK_CHARS)).append(event["data"])
                continue
            for field, buf in parts.items():
                event[field] = "".join(buf)
            parts.clear()
            yield kind, event


def _error(event: dict, session_id: str | None) -> Exception:
    from ..agent_manager import errors
    from ..retry_policy import NON_RETRYABLE_ERRORS

    kind, message = event.get("type"), event.get("message") or ""
    if kind == "TimeoutExpired":
        exc: Exception = subprocess.TimeoutExpired(cmd=message, timeout=event.get("timeout") or 0)
    elif kind == "UsageLimitError":
        reset_at = event.get("reset_at")
        exc = errors.UsageLimitError(message, reset_at=reset_at and datetime.fromisoformat(reset_at))
    elif kind in NON_RETRYABLE_ERRORS:  # the retry policy reads the class name
        exc = (getattr(errors, kind, None) or getattr(builtins, kind))(message)
    else:
        exc = RuntimeError(message)
    exc.session_id = event.get("session_id") or session_id  # type: ignore[attr-defined]
    exc.agent_output = event.get("agent_output")  # type: ignore[attr-defined]
    return exc


class RemoteRunner:
    """A ``Runner`` that runs in a runner service. Same signature as ``create_runner``."""

    def __init__(self, harness: str, context, *, logger: logging.Logger | None = None,
                 dry_run: bool = False):
        self.harness = harness
        self.context = context
        self.logger = logger or logging.getLogger(__name__)
        self.dry_run = dry_run
        self.runner_ref: str | None = None  # "<socket>:<boot id>" of the owner, set before on_pid
        self._on_pid = self._on_session_id = self._on_fragment = None

    def observe(self, *, on_pid=None, on_session_id=None, on_fragment=None) -> None:
        """``on_fragment``: the submit half of the delta seam; unset, nothing is mapped."""
        self._on_pid = on_pid or self._on_pid
        self._on_session_id = on_session_id or self._on_session_id
        self._on_fragment = on_fragment or self._on_fragment

    def prepare_resume(self, session_id: str) -> bool:
        """Only moves files in the run HOME, on the shared /workspace: done here."""
        from ..harness.registry import create_runner

        local = create_runner(self.harness, self.context, logger=self.logger)
        return getattr(local, "prepare_resume", lambda _sid: True)(session_id)

    def execute(self, request):
        name = _pick()
        session_id = terminal = None
        spec = {"op": "execute", "context": self.context, "request": request,
                "dry_run": self.dry_run, "stream": self._on_fragment is not None}
        try:
            for kind, event in _call(name, spec, self.context.timeout_seconds + 60):
                if kind == "pid":
                    self.runner_ref = f"{name}:{event['boot']}"
                    self._callback(self._on_pid, event["pid"])
                elif kind == "session":
                    session_id = event["id"]
                    self._callback(self._on_session_id, session_id)
                elif kind == "fragment":
                    self._callback(self._on_fragment, event["f"])
                elif kind == "usage":
                    self._record_usage(event)
                elif kind in ("result", "error"):
                    terminal = kind, event
                    break
        except (OSError, ValueError) as exc:  # the transport only: run errors raise below
            self.logger.warning("runner %s: %s", name, type(exc).__name__)
        if terminal is None:
            # The runner or connection died, and the run with it; a retry resumes (§31.7).
            terminal = "error", {"type": "RuntimeError", "message": "runner connection lost"}
        kind, event = terminal
        if kind == "result":
            return wire.result_from_wire(event)
        raise _error(event, session_id)

    def _callback(self, fn, value) -> None:
        if fn is not None:
            try:
                fn(value)
            except Exception:
                self.logger.warning("runner callback failed")

    def _record_usage(self, usage: dict) -> None:
        """Best-effort. No credential: ``credential_id = NULL``, by ``(harness, provider)``."""
        from ..agent_manager.usage_store import record_usage
        from ..database_config import DatabaseConfig
        from ..db import get_connection, pooled

        tokens_in, tokens_out = usage["input_tokens"] or 0, usage["output_tokens"] or 0
        try:
            with pooled(DatabaseConfig.from_env(), get_connection) as conn:
                record_usage(conn, credential_id=getattr(self.context.credential, "id", None),
                             tokens_used=tokens_in + tokens_out, input_tokens=tokens_in,
                             output_tokens=tokens_out, duration_ms=usage["duration_ms"] or 0,
                             model=usage["model"], harness=str(self.context.harness),
                             provider=str(self.context.provider), logger=self.logger)
                conn.commit()
        except Exception:
            self.logger.exception("Failed to record usage (best-effort, continuing)")


def run(argv: list[str], *, env: dict[str, str] | None = None, cwd: str | None = None,
        input: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    """``subprocess.run(argv, capture_output=True, text=True)`` in a runner, raising as it
    does; ``env`` is added to the runner's own clean env."""
    request = {"op": "run", "argv": argv, "env": env or {}, "cwd": cwd, "input": input,
               "timeout": timeout}
    for kind, event in _call(_pick(), request, timeout + 30 if timeout else None):
        if kind == "done":
            return subprocess.CompletedProcess(argv, event["rc"], event["stdout"], event["stderr"])
        raise _error(event, None)
    raise ConnectionError("runner connection lost")


def pty(argv: list[str], home: str) -> socket.socket:
    """A CLI in a PTY in a runner. The socket carries its output, then ``\\0<exit code>``;
    what is written to it goes to the CLI. Closing it kills the CLI."""
    sock = _connect(_pick(), 30)
    sock.sendall(wire.encode({"op": "pty", "argv": argv, "home": home}))
    sock.settimeout(None)
    return sock


def _ask(ref: str | None, request: dict) -> dict | None:
    """The owner's reply, or ``None`` when the owner cannot be asked or answered nothing."""
    name = (ref or "").partition(":")[0]
    if _RUNNER_NAME.fullmatch(name):
        with contextlib.suppress(OSError, ValueError):
            return next(_call(name, request, 5), (None, None))[1]
    return None


def alive(ref: str | None, tag: str) -> str:
    """``alive``, ``dead`` or ``unknown`` for the run ``tag`` that ``ref`` started. The
    owner's answer counts only with the recorded boot id; another boot id is ``dead`` (only
    a container start publishes that name); no answer is ``unknown``, which blocks recovery
    and resume, and no timeout turns it into ``dead``. No ``ref``: no runner started it."""
    if not ref:
        return "dead"
    event = _ask(ref, {"op": "alive", "tag": tag})
    if event is None or "boot" not in event:
        return "unknown"
    return event["state"] if event["boot"] == ref.partition(":")[2] else "dead"


def signal(ref: str | None, tag: str, sig: int) -> bool:
    """Send ``sig`` to the run's process group, through its owner only. ``True`` if sent."""
    boot = (ref or "").partition(":")[2]
    event = _ask(ref, {"op": "signal", "tag": tag, "sig": int(sig), "boot": boot})
    return bool(event and event.get("boot") == boot and event.get("sent"))


def shared_tmp() -> Path:
    """A ``0700`` parent for a temporary HOME the runner sees at the same path."""
    from ..workspace_paths import BASE_WORKSPACE_DIR

    path = Path(BASE_WORKSPACE_DIR) / ".tmp"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path
