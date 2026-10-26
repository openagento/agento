"""The runner server: the one place an agent or vendor CLI starts (docs/architecture/runner.md).
One request per connection, NDJSON events back (``wire.py``); every reply carries this boot's
id (the owner rule). Logs carry tags, pids and exit codes only (SEC-6).

It runs as root and runs no harness or module code: each child op gets its own uid
(``uids.py``), and an ``execute`` runs in a worker process as that uid (``worker.py``)."""
from __future__ import annotations

import contextlib
import fcntl
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
from functools import partial

from ..harness.subprocess_runner import collect, harness_base_env
from ..process_hardening import _load_libc
from . import uids, wire
from .worker import finish as _finish

log = logging.getLogger("agento.runner")
BOOT = uuid.uuid4().hex
SIGNALS = frozenset({signal.SIGTERM, signal.SIGKILL})
FAILURE_LOG_LIMIT = 5        # refused connections logged per peer per minute
CHILD_OPS = frozenset({"execute", "run", "pty"})
TERMINAL = frozenset({"result", "error"})
WORKER = [sys.executable, "-m", "agento.framework.runner.worker"]


class Refused(Exception):
    """A child op that cannot start, or whose cleanup is incomplete; its text goes back as
    an error event."""


def _limit(name: str, default: int) -> int:
    return int(os.environ.get(f"AGENTO_RUNNER_{name}", default))


def peer_pid(conn: socket.socket) -> int | None:
    """The peer's pid in this PID namespace: 0 is a process outside it (the worker).
    ``None`` where the kernel has no ``SO_PEERCRED``."""
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    return struct.unpack("3i", conn.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[0]


def spawn_worker(uid: int) -> subprocess.Popen:
    """The run worker as ``uid``; it reads the request on stdin, writes events on stdout."""
    return subprocess.Popen(WORKER, **uids.as_uid(uid), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, start_new_session=True)


class Server:
    """Serves one listening socket until closed. ``peer``/``spawn``/``owner`` are test seams.
    Bounds (SEC-12, SCL-1): children at one time, extra connections for alive/signal,
    authorized connections per second (token bucket), and one uid per child from this
    runner's own range (``index``: the ``i`` of ``runner-<i>.sock``)."""

    def __init__(self, listener: socket.socket, *, index: int = 1, max_procs: int | None = None,
                 max_control: int | None = None, accept_rate: int | None = None,
                 peer: Callable[[socket.socket], int | None] = peer_pid,
                 spawn: Callable[[int], subprocess.Popen] = spawn_worker,
                 owner: tuple[int, int] | None = None, state_dir: str | None = None):
        self._listener = listener
        self._max_procs = max_procs or _limit("MAX_PROCS", 200)
        if self._max_procs > uids.RUN_UID_SLOTS:
            # A larger bound would reach into the next runner's uid range.
            raise ValueError(f"AGENTO_RUNNER_MAX_PROCS is at most {uids.RUN_UID_SLOTS}")
        self._max_open = self._max_procs + (max_control or _limit("MAX_CONTROL", 32))
        self._rate = accept_rate or _limit("ACCEPT_RATE", 500)
        self._peer = peer
        self._spawn = spawn
        self._owner = owner or (os.getuid(), os.getgid())  # who gets a run dir back
        self._state = state_dir  # root-only notes of the live run dirs (uids.reclaim)
        # A uid whose run dir a dead server left and reclaim could not give back stays out.
        kept = uids.reclaim(state_dir, *self._owner) if state_dir else set()
        self._uids = uids.UidPool(u for u in uids.uid_range(index) if u not in kept)
        self._lock = threading.Lock()
        self._procs: dict[str, int] = {}     # tag -> uid of a live run
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
            try:
                with send_lock:
                    conn.sendall(wire.encode(event))
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
                except Refused as exc:
                    event = {"ev": "error", "type": "RuntimeError", "message": str(exc)}
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
            self._children += 1
            return True

    @contextlib.contextmanager
    def _isolated(self, path: str | None, tag: str | None = None):
        """One uid for one child op, and the run dir ``path`` (if any) given to it. On exit,
        in this order: every process of the uid is stopped, the dir goes back to the owner,
        the tag and the uid are free. Only then does the op send its terminal frame, so the
        caller reads the HOME (credential capture) only when it is ``agent``'s again."""
        try:
            real = uids.run_dir(path) if path else None
        except (OSError, ValueError) as exc:
            raise Refused("the run dir is not a directory under /workspace/artifacts or "
                          "/workspace/.tmp") from exc
        uid = self._uids.take()
        if uid is None:  # only when uids were kept after a failed kill
            raise Refused("runner busy: max procs reached")
        if tag:
            with self._lock:
                self._procs[tag] = uid
        try:
            if real:
                uids.record(self._state, uid, real)  # first: a crash in the walk is reclaimed
                try:
                    uids.own(real, uid, uid, for_run=True)
                except (OSError, ValueError) as exc:  # swapped or unreadable since the check
                    raise Refused("the run dir could not be given to the run") from exc
            yield uid
        finally:
            stopped = done = uids.kill_all(uid)
            # A survivor could change the dir during the walk: then the dir and its note stay,
            # and reclaim gives it back at the next start, when nothing of the uid is alive.
            if real and stopped:
                try:
                    uids.own(real, *self._owner, for_run=False, retiring=uid)
                    uids.record(self._state, uid, None)
                except (OSError, ValueError):
                    done = False
            with self._lock:  # with _signal: a signal never reaches the uid's next run
                if tag and stopped:  # a survivor keeps the tag "alive": no resume of it
                    self._procs.pop(tag, None)
                if done:
                    self._uids.give_back(uid)
            if not done:  # the uid and the note stay; the caller gets no success (SEC-9)
                log.warning("runner: cleanup of uid %s is incomplete; it is not used again", uid)
                raise Refused("runner cleanup incomplete")

    @staticmethod
    def _watch_eof(conn: socket.socket, run: dict) -> None:
        """The worker closed the connection (it died, or gave up): kill the run."""
        with contextlib.suppress(OSError):
            while conn.recv(4096):
                pass
        run["eof"] = True  # read by the relay: a run that starts after this is killed there
        _kill(run)

    # -- ops: each returns its terminal event, sent after its child slot is free ------

    def _execute(self, conn, req, send) -> dict:
        ctx = req.get("context") or {}
        tag = ctx.get("tag")
        # The run dir is the HOME. A job with no agent_view gets a temp HOME under
        # `/workspace/.tmp` from the consumer, never an owner change of `/workspace` itself.
        with self._isolated(ctx.get("home_dir"), tag) as uid:
            proc = self._spawn(uid)
            run: dict = {"proc": proc}
            threading.Thread(target=self._watch_eof, args=(conn, run), daemon=True).start()
            try:
                stdin = proc.stdin
                assert stdin is not None  # spawned with stdin=PIPE
                with contextlib.suppress(BrokenPipeError):
                    stdin.write(wire.encode(req))
                    stdin.close()
                event = self._relay(proc, run, send, tag)
            finally:
                # Before the cleanup: the EOF watcher must not kill a process group id the
                # kernel may have reused once the run is gone.
                run.pop("pid", None)
        proc.wait()
        log.info("runner: finished tag=%s %s", tag, event["ev"])
        return event

    @staticmethod
    def _relay(proc, run: dict, send, tag) -> dict:
        """Relay the worker's frames and keep its terminal frame (or make one)."""
        for line in iter(partial(proc.stdout.readline, wire.MAX_FRAME + 1), b""):
            if not line.endswith(b"\n"):
                break  # larger than a frame: the worker is broken, the cleanup kills it
            try:
                event = json.loads(line)
            except ValueError:
                break
            if event.get("ev") in TERMINAL:
                return event
            if event.get("ev") == "pid":
                run["pid"] = event["pid"]
                event["boot"] = BOOT
                if run.get("eof"):  # the worker went away before the child started
                    _kill(run)
                log.info("runner: started tag=%s pid=%s", tag, event["pid"])
            send(event)
        return {"ev": "error", "type": "RuntimeError", "message": "the run worker ended with no result"}

    def _run(self, conn, req, send) -> dict:
        """``subprocess.run``, with the bounds and the group kill of a harness run. Its run
        dir is the ``HOME`` it is given, else its ``cwd``."""
        env = {**harness_base_env(), **(req.get("env") or {})}
        with self._isolated((req.get("env") or {}).get("HOME") or req.get("cwd")) as uid:
            try:
                proc = subprocess.Popen(
                    req["argv"], cwd=req.get("cwd"), env=env, **uids.as_uid(uid),
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
        CLI's NULs are dropped, so ``\\0<exit code>`` at the end is unambiguous; it is sent
        after the cleanup."""
        argv, home = req["argv"], req["home"]
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": home,
               "LANG": os.environ.get("LANG", "C.UTF-8"), "TERM": "dumb"}
        code = None
        with self._isolated(home) as uid:
            libc = _load_libc()  # before the fork: the child calls no dynamic loader
            pid, fd = os.forkpty()
            if pid == 0:  # child
                try:
                    if libc is not None:
                        libc.prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
                    # Wide, so a long login URL is printed on one line and not wrapped.
                    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 4000, 0, 0))
                    uids.drop_to(uid)
                    os.chdir(home)
                    os.execvpe(argv[0], argv, env)
                finally:
                    os._exit(127)
            code = self._relay_pty(conn, pid, fd)
        if code is not None:
            with contextlib.suppress(OSError):
                conn.sendall(b"\0" + str(code).encode())

    @staticmethod
    def _relay_pty(conn, pid: int, fd: int) -> int | None:
        """Bytes both ways until the CLI ends (its exit code) or the worker closes (None)."""
        reaped = False
        try:
            while True:
                ready, _, _ = select.select([fd, conn], [], [])
                if conn in ready:
                    if not (data := conn.recv(4096)):
                        return None  # the worker closed: kill the CLI
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
            return os.waitstatus_to_exitcode(status)
        except OSError:
            return None
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
        """Only for this boot: an older boot's ref never reaches a run with the same tag.
        The signal goes to every process of the run's uid (``uids.signal_uid``)."""
        sig = int(req.get("sig", 0))
        sent = False
        if sig in SIGNALS and req.get("boot") == BOOT:
            # ponytail: a short `kill` child under the lock, so the uid cannot be given to
            # the next run meanwhile; signals are rare. Per-uid locks if that shows up.
            with self._lock:
                uid = self._procs.get(req.get("tag"))
                sent = uid is not None and uids.signal_uid(uid, sig)
        return {"ev": "signal", "sent": sent, "boot": BOOT}


def _kill(run: dict) -> None:
    """The worker, and the run's process group if it started one (``SIGKILL``)."""
    if proc := run.get("proc"):
        with contextlib.suppress(OSError):
            proc.kill()
    if pid := run.get("pid"):
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGKILL)


def _index(socket_path: str) -> int:
    """``i`` of ``.../runner-<i>.sock``: which uid range is this runner's."""
    name = os.path.basename(socket_path)
    if not (name.startswith("runner-") and name.endswith(".sock") and name[7:-5].isdigit()):
        raise ValueError("AGENTO_RUNNER_SOCKET must end in runner-<i>.sock")
    return int(name[7:-5])


def main() -> None:
    """``python -m agento.framework.runner.server``, as root (only the compose capabilities),
    under the container's init, with the listening socket on fd 3 (``listen.py``)."""
    import pwd

    from ..process_hardening import make_non_dumpable

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    make_non_dumpable()  # it holds co-tenant runs' secrets: no peer reads its heap
    if os.environ.get("LISTEN_FDS") != "1" or os.getuid() != 0:
        sys.exit("runner: needs LISTEN_FDS=1 (listen.py) and root (it starts each run as its own uid)")
    os.set_inheritable(3, False)  # no child holds the listener
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))  # runs die with the namespace
    path = os.environ.get("AGENTO_RUNNER_SOCKET", "runner-1.sock")
    agent = pwd.getpwnam("agent")
    owner = (agent.pw_uid, agent.pw_gid)
    try:
        uids.set_shared_groups(uids.parse_shared_groups(os.environ.get("AGENTO_RUN_GROUPS", ""), agent.pw_gid))
    except ValueError as exc:
        sys.exit(f"runner: {exc}")
    state = os.path.join(os.path.dirname(path), "live")  # the socket's root-owned volume
    log.info("runner: serving %s boot=%s", path, BOOT)
    Server(socket.socket(fileno=3), index=_index(path), owner=owner,
           state_dir=state).serve_forever()


if __name__ == "__main__":
    main()
