"""The runner server: the one place an agent or vendor CLI starts (docs/architecture/runner.md).
One request per connection, NDJSON events back (``wire.py``); every reply carries this boot's
id (the owner rule). Logs carry tags, pids and exit codes only (SEC-6)."""
from __future__ import annotations

import contextlib
import fcntl
import inspect
import json
import logging
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from functools import partial

from ..harness.registry import create_runner, get_harness
from ..harness.runtime import RunRequest
from ..harness.subprocess_runner import collect, harness_base_env
from ..process_hardening import _load_libc
from ..retry_policy import NON_RETRYABLE_ERRORS
from . import wire

log = logging.getLogger("agento.runner")
BOOT = uuid.uuid4().hex
# Raised to the worker by name; anything else arrives as a RuntimeError. Every name the
# retry policy reads must keep its name, or a terminal error is retried.
ERRORS = NON_RETRYABLE_ERRORS | {"TimeoutExpired", "RuntimeError"}
SIGNALS = frozenset({signal.SIGTERM, signal.SIGKILL})
FAILURE_LOG_LIMIT = 5        # refused connections logged per peer per minute
CHILD_OPS = frozenset({"execute", "run", "pty"})
USAGE = ("input_tokens", "output_tokens", "duration_ms", "model")


def _limit(name: str, default: int) -> int:
    return int(os.environ.get(f"AGENTO_RUNNER_{name}", default))


def peer_pid(conn: socket.socket) -> int | None:
    """The peer's pid in this PID namespace: 0 is a process outside it (the worker).
    ``None`` where the kernel has no ``SO_PEERCRED``."""
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    return struct.unpack("3i", conn.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[0]


class Server:
    """Serves one listening socket until closed. ``peer``/``make_runner`` are test seams.
    Bounds (SEC-12, SCL-1): children at one time, extra connections for alive/signal,
    authorized connections per second (token bucket)."""

    def __init__(self, listener: socket.socket, *, max_procs: int | None = None,
                 max_control: int | None = None, accept_rate: int | None = None,
                 peer: Callable[[socket.socket], int | None] = peer_pid,
                 make_runner=None, reload: Callable[[], None] | None = None):
        self._listener = listener
        self._max_procs = max_procs or _limit("MAX_PROCS", 200)
        self._max_open = self._max_procs + (max_control or _limit("MAX_CONTROL", 32))
        self._rate = accept_rate or _limit("ACCEPT_RATE", 500)
        self._peer = peer
        self._make_runner = make_runner or partial(create_runner, logger=log)
        self._reload = reload
        self._lock = threading.Lock()
        self._procs: dict[str, int] = {}     # tag -> pid (= process group) of a live run
        self._open = self._children = 0
        self._tokens = float(self._rate)
        self._refill = time.monotonic()
        self._failures: dict[int | None, tuple[float, int]] = {}

    def serve_forever(self) -> None:
        """The peer check, then the two limiters, before any op (SEC-12)."""
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return  # the listener was closed
            try:
                pid = self._peer(conn)
            except OSError:
                pid = None
            if pid != 0:
                self._refuse(conn, pid)
                continue
            with self._lock:
                ok = self._open < self._max_open and self._take_token()
                self._open += ok
            if ok:
                threading.Thread(target=self._serve, args=(conn,), daemon=True).start()
            else:
                conn.close()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._listener.shutdown(socket.SHUT_RDWR)  # wakes accept() on Linux
        self._listener.close()

    def _refuse(self, conn: socket.socket, pid: int | None) -> None:
        """A peer inside this namespace (an agent) gets nothing; logged per pid to a limit."""
        conn.close()
        now = time.monotonic()
        start, count = self._failures.get(pid, (now, 0))
        if now - start > 60:
            start, count = now, 0
        if len(self._failures) >= 1024:
            self._failures.clear()
        self._failures[pid] = (start, count + 1)
        if count < FAILURE_LOG_LIMIT:
            log.warning("runner: refused a peer inside the runner (pid=%s)", pid)

    def _take_token(self) -> bool:
        rate = self._rate
        now = time.monotonic()
        self._tokens = min(float(rate), self._tokens + (now - self._refill) * rate)
        self._refill = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True

    def _serve(self, conn: socket.socket) -> None:
        send_lock = threading.Lock()

        def send(event: dict) -> None:
            line = wire.encode(event)
            if len(line) > wire.MAX_FRAME and event.get("ev") == "fragment":
                line = wire.encode(_cut(event))
            try:
                with send_lock:
                    conn.sendall(line)
            except OSError:
                pass  # the worker went away; the EOF watcher kills the run

        try:
            conn.settimeout(30)
            line = conn.makefile("rb").readline(wire.MAX_REQUEST + 1)
            try:
                if len(line) > wire.MAX_REQUEST or not line.endswith(b"\n"):
                    raise ValueError
                req = json.loads(line)
                name = req["op"]
                op = {"execute": self._execute, "run": self._run, "pty": self._pty,
                      "alive": self._alive, "signal": self._signal}[name]
            except (ValueError, TypeError, KeyError):
                return send({"ev": "error", "type": "protocol", "message": "bad request"})
            conn.settimeout(None)
            if name not in CHILD_OPS:
                event = op(conn, req, send)
            elif not self._child_slot():
                event = {"ev": "error", "type": "RuntimeError",
                         "message": "runner busy: max procs reached"}
            else:
                try:
                    event = op(conn, req, send)
                finally:
                    with self._lock:
                        self._children -= 1
            if event:
                _finish(send, event)
        except Exception as exc:
            log.warning("runner: op failed (%s)", type(exc).__name__)
        finally:
            conn.close()
            with self._lock:
                self._open -= 1

    def _child_slot(self) -> bool:
        with self._lock:
            if self._children >= self._max_procs:
                return False
            if self._children == 0 and self._reload is not None:
                # ponytail: reload under the lock, so accept waits for one bootstrap after
                # idle; move it out with a reload-in-progress flag if that shows up.
                self._reload()
            self._children += 1
            return True

    @staticmethod
    def _watch_eof(conn: socket.socket, run: dict) -> None:
        """The worker closed the connection (it died, or gave up): kill the run."""
        with contextlib.suppress(OSError):
            while conn.recv(4096):
                pass
        run["eof"] = True  # read by on_pid: a run that starts after this is killed there
        if pid := run.get("pid"):
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGKILL)

    # -- ops: each returns its terminal event, sent after its child slot is free ------

    def _execute(self, conn, req, send) -> dict:
        ctx = wire.context_from_wire(req["context"])
        run: dict = {}
        tag = ctx.tag

        def on_pid(pid: int) -> None:
            run["pid"] = pid
            if run.get("eof"):  # the worker went away before the child started
                with contextlib.suppress(OSError):
                    os.killpg(pid, signal.SIGKILL)
            if tag:
                with self._lock:
                    self._procs[tag] = pid
            log.info("runner: started tag=%s pid=%s", tag, pid)
            send({"ev": "pid", "pid": pid, "boot": BOOT})

        try:  # a runner that cannot start is an error event too, never a lost connection
            runner = self._make_runner(ctx.harness, ctx, dry_run=bool(req.get("dry_run")))
            runner.observe(**_accepted(runner.observe, {
                "on_pid": on_pid, "on_session_id": lambda sid: send({"ev": "session", "id": sid}),
                "on_line": _mapper(ctx.harness, send) if req.get("stream") else None,
                "on_usage": lambda r: send({"ev": "usage", **{k: getattr(r, k) for k in USAGE}})}))
            threading.Thread(target=self._watch_eof, args=(conn, run), daemon=True).start()
            event = {"ev": "result", **asdict(runner.execute(RunRequest(**req["request"])))}
        except Exception as exc:
            event = _error_event(exc)
        finally:
            # Before the terminal event: once the worker has it, it closes, and the EOF
            # watcher must not kill a process group id the kernel may have reused.
            run.pop("pid", None)
            if tag:
                with self._lock:
                    self._procs.pop(tag, None)
        log.info("runner: finished tag=%s %s", tag, event["ev"])
        return event

    def _run(self, conn, req, send) -> dict:
        """``subprocess.run``, with the bounds and the group kill of a harness run."""
        try:
            proc = subprocess.Popen(
                req["argv"], cwd=req.get("cwd"), env={**harness_base_env(), **(req.get("env") or {})},
                stdin=subprocess.DEVNULL if req.get("input") is None else subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
        except FileNotFoundError:
            return {"ev": "error", "type": "FileNotFoundError", "message": "command not found"}
        run = {"pid": proc.pid}
        threading.Thread(target=self._watch_eof, args=(conn, run), daemon=True).start()
        stdout, stderr, timed_out = collect(proc, req.get("input"), req.get("timeout"))
        run.pop("pid")  # as in _execute: the group is dead, its id may be reused
        if timed_out:
            return {"ev": "error", "type": "TimeoutExpired", "timeout": req.get("timeout")}
        return {"ev": "done", "rc": proc.returncode, "stdout": stdout, "stderr": stderr}

    def _pty(self, conn, req, send) -> None:
        """A CLI on a new PTY with ``HOME`` and a minimal env, bytes relayed both ways. The
        CLI's NULs are dropped, so ``\\0<exit code>`` at the end is unambiguous."""
        argv, home = req["argv"], req["home"]
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": home,
               "LANG": os.environ.get("LANG", "C.UTF-8"), "TERM": "dumb"}
        libc = _load_libc()  # before the fork: the child calls no dynamic loader
        pid, fd = os.forkpty()
        if pid == 0:  # child
            try:
                if libc is not None:
                    libc.prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
                # Wide, so a long login URL is printed on one line and not wrapped.
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 4000, 0, 0))
                os.chdir(home)
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(127)
        reaped = False
        try:
            while True:
                ready, _, _ = select.select([fd, conn], [], [])
                if conn in ready:
                    if not (data := conn.recv(4096)):
                        return  # the worker closed: kill the CLI
                    os.write(fd, data)
                if fd in ready:
                    try:
                        data = os.read(fd, 4096)
                    except OSError:  # EIO: the child closed the slave side
                        data = b""
                    if not data:
                        break
                    conn.sendall(data.replace(b"\0", b""))
            _, status = os.waitpid(pid, 0)
            reaped = True
            conn.sendall(b"\0" + str(os.waitstatus_to_exitcode(status)).encode())
        except OSError:
            pass
        finally:
            os.close(fd)
            with contextlib.suppress(OSError):  # ESRCH: only a zombie is left
                os.killpg(pid, signal.SIGKILL)  # forkpty: its own session and group
            if not reaped:
                with contextlib.suppress(OSError):
                    os.waitpid(pid, 0)

    def _alive(self, conn, req, send) -> dict:
        with self._lock:
            state = "alive" if req.get("tag") in self._procs else "dead"
        return {"ev": "alive", "state": state, "boot": BOOT}

    def _signal(self, conn, req, send) -> dict:
        """Only for this boot: an older boot's ref never reaches a run with the same tag."""
        sig = int(req.get("sig", 0))
        with self._lock:
            pid = self._procs.get(req.get("tag"))
        sent = False
        if pid and sig in SIGNALS and req.get("boot") == BOOT:
            with contextlib.suppress(OSError):
                os.killpg(pid, sig)
                sent = True
        return {"ev": "signal", "sent": sent, "boot": BOOT}


def _finish(send, event: dict) -> None:
    """Long text fields go first as ordered ``output`` chunks; no frame exceeds MAX_FRAME."""
    for key, value in list(event.items()):
        if isinstance(value, str) and len(value) > wire.CHUNK_CHARS:
            for i in range(0, len(value), wire.CHUNK_CHARS):
                send({"ev": "output", "field": key, "data": value[i:i + wire.CHUNK_CHARS]})
            del event[key]
    send(event)


def _cut(event: dict) -> dict:
    """A fragment larger than one frame: its text is cut and it says so."""
    f = event["f"]
    text = f.get("text")
    return {"ev": "fragment", "f": {"kind": f.get("kind"), "tool_name": f.get("tool_name"),
                                    "text": text[:wire.CHUNK_CHARS] if isinstance(text, str) else None,
                                    "truncated": True}}


def _mapper(harness: str, send):
    """The map half of the delta seam; the worker keeps the submit half (seq, redaction)."""
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


def _reloader() -> Callable[[], None]:
    """Re-bootstrap when ``modules.json`` changed (core module code needs a restart)."""
    from ..bootstrap import bootstrap
    from ..module_status import _resolve_path

    seen = [None]

    def reload() -> None:
        try:
            mtime = _resolve_path().stat().st_mtime
        except OSError:
            mtime = None
        if mtime != seen[0]:
            seen[0] = mtime
            bootstrap(db_conn=None, quiet=True)

    return reload


def main() -> None:
    """``python -m agento.framework.runner.server``, as ``agent``, under the container's init,
    with the listening socket on fd 3 (``listen.py``)."""
    from .. import db
    from ..process_hardening import make_non_dumpable

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    make_non_dumpable()  # it holds co-tenant runs' secrets: no same-uid peer reads its heap
    if os.environ.get("LISTEN_FDS") != "1" or os.getuid() == 0:
        sys.exit("runner: needs LISTEN_FDS=1 (listen.py) and a non-root user")
    os.set_inheritable(3, False)  # no child holds the listener
    db.DISABLED = True  # no database here: a module observer that tries one fails fast
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))  # runs die with the namespace
    reload = _reloader()
    reload()
    log.info("runner: serving %s boot=%s", os.environ.get("AGENTO_RUNNER_SOCKET", "fd 3"), BOOT)
    Server(socket.socket(fileno=3), reload=reload).serve_forever()


if __name__ == "__main__":
    main()
