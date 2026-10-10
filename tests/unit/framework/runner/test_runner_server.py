"""The runner service (WS5): wire, peer check, SEC-12 limiters, SCL-1 bounds, the owner
rule and kill on disconnect. Real sockets and children; the harness is a seam."""
from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from datetime import datetime

import pytest

from agento.framework.agent_manager.errors import UsageLimitError
from agento.framework.agent_manager.models import CredentialRecord
from agento.framework.harness import HarnessRunContext, RunRequest, RunResult, subprocess_runner
from agento.framework.harness.runtime import McpInitReport, McpServerStatus
from agento.framework.harness.subprocess_runner import SubprocessRunner
from agento.framework.retry_policy import NON_RETRYABLE_ERRORS
from agento.framework.runner import client, server, wire
from agento.framework.runner.client import RemoteRunner

BIG = 3 * wire.MAX_FRAME  # larger than one frame: must arrive as chunks


@pytest.fixture(autouse=True)
def _no_usage_db(monkeypatch):
    monkeypatch.setattr(RemoteRunner, "_record_usage", lambda self, event: None)


def _ctx(tmp_path, tag="job:1", **kw) -> HarnessRunContext:
    return HarnessRunContext(harness="claude", provider="anthropic", working_dir=str(tmp_path),
                             credential_required=False, tag=tag, timeout_seconds=30, **kw)


class _Child:
    """A run that is one plain child in its own process group (as SubprocessRunner)."""

    def __init__(self, argv, *, output="", error=None):
        self.argv, self.output, self.error = argv, output, error
        self.on_pid = self.on_session_id = self.on_line = self.on_usage = None

    def observe(self, **kw):
        for name, fn in kw.items():
            setattr(self, name, fn)

    def execute(self, request):
        proc = subprocess.Popen(self.argv, start_new_session=True)
        self.on_pid(proc.pid)
        self.on_session_id("s-1")
        proc.wait()
        self.on_usage(RunResult(raw_output="", input_tokens=3, output_tokens=2, model="m"))
        if self.error is not None:
            raise self.error
        return RunResult(raw_output=self.output, session_id="s-1")


def _child(argv, **kw):
    return lambda harness, ctx, dry_run: _Child(argv, **kw)


def _wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:  # a zombie of the in-process server counts as gone
        return os.waitpid(pid, os.WNOHANG)[0] == pid
    except ChildProcessError:
        return True


def _raw_execute(tmp_path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(client.socket_path("runner-1.sock"))
    sock.sendall(wire.encode({"op": "execute", "context": _ctx(tmp_path),
                              "request": RunRequest(prompt="p"), "stream": False}))
    return sock


# --- wire ----------------------------------------------------------------------------

def test_a_context_and_a_result_round_trip_with_their_types():
    cred = CredentialRecord(id=3, scope="anthropic", type="oauth", label="a",
                            credentials={"subscription_key": "sk"},
                            expires_at=datetime(2026, 1, 2, 3, 4, 5))
    ctx = HarnessRunContext(harness="claude", provider="anthropic", credential=cred,
                            extra_env={"A": "1"}, harness_config={"x": "y"}, tag="job:9")
    result = RunResult(raw_output="o", mcp_init=McpInitReport(servers=(McpServerStatus("t", "ok"),)))

    back = wire.context_from_wire(json.loads(wire.encode(ctx)))
    res = wire.result_from_wire(json.loads(wire.encode(result)))

    assert back == ctx and back.credential.expires_at == cred.expires_at
    assert res == result


# --- execute end to end, through the real harness ------------------------------------

@pytest.mark.usefixtures("builtin_harnesses")
def test_a_harness_run_goes_through_the_socket_whole(runner_server, tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json
        print(json.dumps({{"type": "system", "subtype": "init", "session_id": "s-1",
                          "mcp_servers": [{{"name": "toolbox", "status": "connected"}}]}}), flush=True)
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                          "result": "x" * {BIG}, "session_id": "s-1", "num_turns": 1,
                          "duration_ms": 5, "usage": {{"input_tokens": 3, "output_tokens": 2}}}}))
    """))
    (bindir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    usage = []
    monkeypatch.setattr(RemoteRunner, "_record_usage", lambda self, event: usage.append(event))
    runner = RemoteRunner("claude", _ctx(tmp_path))
    seen = []
    runner.observe(on_pid=lambda pid: seen.append(runner.runner_ref),
                   on_session_id=lambda sid: seen.append(sid))

    result = runner.execute(RunRequest(prompt="hi"))

    assert seen == [f"runner-1.sock:{server.BOOT}", "s-1"]     # the owner is known at on_pid
    assert len(result.raw_output) >= BIG and result.session_id == "s-1"
    assert result.mcp_init == McpInitReport(servers=(McpServerStatus("toolbox", "connected"),))
    assert [(u["input_tokens"], u["output_tokens"]) for u in usage] == [(3, 2)]


# --- errors and their output ----------------------------------------------------------

def test_a_timeout_comes_back_as_a_timeout_with_its_whole_output(start_runner, tmp_path):
    exc = subprocess.TimeoutExpired(cmd="claude", timeout=7)
    exc.agent_output = "y" * BIG
    exc.session_id = "s-1"
    start_runner(make_runner=_child(["true"], error=exc))

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        RemoteRunner("claude", _ctx(tmp_path)).execute(RunRequest(prompt="p"))

    assert raised.value.agent_output == "y" * BIG and raised.value.session_id == "s-1"


def test_a_usage_limit_keeps_its_reset_time(start_runner, tmp_path):
    start_runner(make_runner=_child(["true"], error=UsageLimitError(
        "limit", reset_at=datetime(2026, 5, 1, 12, 0))))

    with pytest.raises(UsageLimitError) as raised:
        RemoteRunner("claude", _ctx(tmp_path)).execute(RunRequest(prompt="p"))

    assert raised.value.reset_at == datetime(2026, 5, 1, 12, 0)


@pytest.mark.parametrize("name", [*sorted(NON_RETRYABLE_ERRORS), "LookupError"])
def test_every_error_the_retry_policy_names_keeps_its_name(start_runner, tmp_path, name):
    """CLS-1: the retry policy reads the class name; a renamed error is retried. Any
    other error is a RuntimeError."""
    import builtins

    from agento.framework.agent_manager import errors

    cls = getattr(errors, name, None) or getattr(builtins, name)
    start_runner(make_runner=_child(["true"], error=cls("x")))

    with pytest.raises(Exception) as raised:
        RemoteRunner("claude", _ctx(tmp_path)).execute(RunRequest(prompt="p"))

    assert type(raised.value).__name__ == (name if name in NON_RETRYABLE_ERRORS else "RuntimeError")


# --- the owner rule and kill on disconnect ------------------------------------------------

def test_alive_and_signal_answer_only_for_the_owner_and_its_boot(start_runner, tmp_path):
    start_runner(make_runner=_child(["sleep", "30"]))
    runner = RemoteRunner("claude", _ctx(tmp_path))
    pids = []
    runner.observe(on_pid=pids.append)
    box = {}
    thread = threading.Thread(daemon=True, target=lambda: box.update(
        result=runner.execute(RunRequest(prompt="p"))))
    thread.start()
    assert _wait(lambda: pids)
    ref = runner.runner_ref

    assert client.alive(ref, "job:1") == "alive"
    assert client.alive(ref, "job:2") == "dead"
    assert client.alive(ref.split(":")[0] + ":old-boot", "job:1") == "dead"   # restarted
    assert client.alive("runner-9.sock:" + server.BOOT, "job:1") == "unknown"  # cannot ask
    assert client.alive("../x.sock:" + server.BOOT, "job:1") == "unknown"      # not a name
    assert client.alive(None, "job:1") == "dead"
    assert client.signal(ref.split(":")[0] + ":old-boot", "job:1", signal.SIGTERM) is False
    assert client.signal(ref, "job:1", signal.SIGHUP) is False                # not allowed
    assert client.alive(ref, "job:1") == "alive"                              # neither was sent

    assert client.signal(ref, "job:1", signal.SIGTERM) is True
    thread.join(10)
    assert _gone(pids[0])
    assert client.alive(ref, "job:1") == "dead"
    assert "result" in box


def test_a_closed_connection_kills_the_run(start_runner, tmp_path):
    start_runner(make_runner=_child(["sleep", "30"]))
    sock = _raw_execute(tmp_path)
    pid = json.loads(sock.makefile("rb").readline())["pid"]

    sock.close()   # the worker died

    assert _wait(lambda: _gone(pid))


def test_a_run_that_starts_after_the_worker_left_is_killed(start_runner, tmp_path):
    pids = []

    class _Late(_Child):
        def execute(self, request):
            time.sleep(0.5)                  # the worker closes in this window
            proc = subprocess.Popen(self.argv, start_new_session=True)
            pids.append(proc.pid)
            self.on_pid(proc.pid)
            proc.wait()
            return RunResult(raw_output="")

    start_runner(make_runner=lambda h, ctx, dry_run: _Late(["sleep", "30"]))
    _raw_execute(tmp_path).close()

    assert _wait(lambda: pids) and _wait(lambda: _gone(pids[0]))


def test_a_lost_runner_is_a_retryable_error_with_the_session(start_runner, tmp_path):
    """No terminal event (the runner died): the error carries the session for resume."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(client.socket_path("runner-1.sock"))
    listener.listen(1)

    def die_mid_run():
        conn, _ = listener.accept()
        conn.makefile("rb").readline()
        conn.sendall(wire.encode({"ev": "pid", "pid": 1, "boot": "b"})
                     + wire.encode({"ev": "session", "id": "s-1"}))
        conn.close()

    threading.Thread(target=die_mid_run, daemon=True).start()
    with pytest.raises(RuntimeError, match="connection lost") as raised:
        RemoteRunner("claude", _ctx(tmp_path)).execute(RunRequest(prompt="p"))
    listener.close()

    assert raised.value.session_id == "s-1"


# --- SEC-12: peer check and the two limiters ---------------------------------------------

def test_a_peer_inside_the_runner_gets_nothing(start_runner, tmp_path):
    start_runner(peer=lambda conn: 4242, make_runner=_child(["true"]))

    assert client.alive("runner-1.sock:" + server.BOOT, "job:1") == "unknown"
    with pytest.raises(RuntimeError, match="connection lost"):
        RemoteRunner("claude", _ctx(tmp_path)).execute(RunRequest(prompt="p"))


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="SO_PEERCRED is Linux only")
def test_the_real_peer_check_refuses_a_process_in_this_namespace(start_runner):
    start_runner(peer=server.peer_pid)

    assert client.alive("runner-1.sock:" + server.BOOT, "job:1") == "unknown"


def test_peer_pid_is_none_without_so_peercred(monkeypatch):
    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)

    assert server.peer_pid(None) is None


def test_a_flood_of_refused_peers_neither_starves_the_worker_nor_floods_the_log(
        start_runner, tmp_path, caplog):
    """10x the accept rate of refused peers while 100 authorized calls all succeed; the
    log keeps FAILURE_LOG_LIMIT lines per peer (a peer gone before its check is `None`)."""
    bad_dir = tempfile.mkdtemp(prefix="rb", dir="/tmp")

    def peer(conn):  # a bound client path marks the refused peer
        return 4242 if conn.getpeername() else 0

    start_runner(peer=peer, accept_rate=50)
    path = client.socket_path("runner-1.sock")
    stop = threading.Event()

    def flood():
        for i in range(500):
            if stop.is_set():
                return
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.bind(os.path.join(bad_dir, str(i)))
                s.connect(path)
            except OSError:
                pass
            finally:
                s.close()

    attacker = threading.Thread(target=flood, daemon=True)
    with caplog.at_level("WARNING", logger="agento.runner"):
        attacker.start()
        results = []
        for _ in range(100):
            results.append(client.run(["true"], timeout=10).returncode)
            time.sleep(0.02)   # 100 calls in ~2 s: under the 50/s accept rate
        stop.set()
        attacker.join(10)

    assert results == [0] * 100
    refused = [r.args for r in caplog.records if "refused" in r.getMessage()]
    assert refused and all(refused.count(p) <= server.FAILURE_LOG_LIMIT for p in refused)


def test_the_accept_rate_bounds_authorized_connections(start_runner):
    start_runner(accept_rate=5)
    answers = [client.alive("runner-1.sock:" + server.BOOT, "job:1") for _ in range(20)]

    assert answers.count("dead") <= 6 and "unknown" in answers


def test_max_procs_refuses_the_next_child(start_runner, tmp_path):
    start_runner(max_procs=1, max_control=4)
    busy = client.pty(["sleep", "30"], str(tmp_path))
    try:
        assert _wait(lambda: client.alive("runner-1.sock:" + server.BOOT, "x") == "dead")
        with pytest.raises(RuntimeError, match="busy"):
            client.run(["true"], timeout=10)
    finally:
        busy.close()


def test_a_request_line_over_the_limit_is_refused(runner_server):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(client.socket_path("runner-1.sock"))
    with contextlib.suppress(OSError):   # the server may close first
        sock.sendall(b"x" * (wire.MAX_REQUEST + 10) + b"\n")
    line = sock.makefile("rb").readline()
    sock.close()

    assert line == b"" or json.loads(line)["type"] == "protocol"


# --- run: a vendor CLI that is not a run --------------------------------------------------

def test_run_returns_output_and_gives_the_env_to_the_child(runner_server):
    proc = client.run([sys.executable, "-c",
                       "import os,sys; print(os.environ.get('HOME')); "
                       f"sys.stdout.write('z' * {BIG})"],
                      env={"HOME": "/h"}, timeout=30)

    lines = proc.stdout.split("\n")
    assert (proc.returncode, lines[0]) == (0, "/h")
    assert len(lines[1]) == BIG


def test_run_raises_as_subprocess_run_does(runner_server):
    with pytest.raises(FileNotFoundError):
        client.run(["/no/such/cli"], timeout=10)
    with pytest.raises(subprocess.TimeoutExpired):
        client.run(["sleep", "5"], timeout=0.2)


# --- the contract, the process group and the output bounds (review round 1) ---------------

def test_a_runner_with_only_the_published_observe_runs(start_runner, tmp_path):
    """CODE-5: ``Runner.observe`` publishes on_pid/on_session_id only."""
    class _Strict:
        def observe(self, *, on_pid=None, on_session_id=None) -> None: ...

        def execute(self, request):
            return RunResult(raw_output="ok")

    start_runner(make_runner=lambda h, ctx, dry_run: _Strict())

    assert RemoteRunner("claude", _ctx(tmp_path)).execute(RunRequest(prompt="p")).raw_output == "ok"


class _Sh(SubprocessRunner):
    _parse_output = staticmethod(lambda raw: RunResult(raw_output=raw))
    _credential_env = staticmethod(lambda credential: {})


def _spawn(op, argv, tmp_path, timeout):
    """``argv`` as a harness run (SubprocessRunner) or as a ``run`` op; its stdout."""
    if op == "run":
        return client.run(argv, timeout=timeout).stdout
    ctx = HarnessRunContext(harness="sh", provider="sh", working_dir=str(tmp_path),
                            credential_required=False, timeout_seconds=timeout)
    builder = type("B", (), {"headless": lambda self, c, r: argv})()
    return _Sh(context=ctx, command_builder=builder).execute(RunRequest(prompt="")).raw_output


def _dead(pid: int) -> bool:
    """Gone or a zombie, by ``ps`` (procps): this pid is no child of the test process."""
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return stat.stdout.strip()[:1] in ("", "Z")


@pytest.mark.parametrize("op", ["execute", "run", "pty"])
def test_no_descendant_outlives_its_run(runner_server, tmp_path, op):
    """A descendant that keeps the pipes open (and ignores the PTY hangup) dies with the run:
    on timeout, or when the worker is gone."""
    pid_file = tmp_path / "pid"
    argv = ["sh", "-c", f"trap '' HUP; sleep 60 & echo $! > {pid_file}; sleep 60"]
    if op == "pty":
        sock = client.pty(argv, str(tmp_path))
        assert _wait(lambda: pid_file.exists() and pid_file.read_text().strip())
        sock.close()
    else:
        with pytest.raises(subprocess.TimeoutExpired):
            _spawn(op, argv, tmp_path, 1)

    assert _wait(lambda: _dead(int(pid_file.read_text())))


@pytest.mark.parametrize("op", ["execute", "run"])
def test_output_is_kept_to_its_bound_also_as_one_line(runner_server, tmp_path, monkeypatch, op):
    """SCL-1: one line with no newline is read in pieces; only the tail is kept."""
    monkeypatch.setattr(subprocess_runner, "MAX_RETAINED", 1000)

    out = _spawn(op, [sys.executable, "-c", "import sys; sys.stdout.write('x' * 5000 + 'END')"],
                 tmp_path, 30)

    assert out.endswith("END") and len(out) <= 1000


def test_no_frame_exceeds_the_limit_for_any_text():
    """A non-BMP char is 12 JSON bytes (a surrogate pair)."""
    frames = []
    server._finish(lambda e: frames.append(wire.encode(e)),
                   {"ev": "done", "stdout": "\U0001F600" * (wire.CHUNK_CHARS + 1)})

    assert len(frames) > 1 and max(map(len, frames)) <= wire.MAX_FRAME


# --- SEC-6: no secret in the runner's log -------------------------------------------------

def test_the_log_has_no_prompt_env_or_credential(start_runner, tmp_path, caplog):
    start_runner(make_runner=_child(["true"], output="ok"))
    cred = CredentialRecord(id=1, scope="anthropic", type="oauth", label="a",
                            credentials={"subscription_key": "SECRET-CRED"})
    ctx = _ctx(tmp_path, credential=cred, extra_env={"TOKEN": "SECRET-ENV"})

    with caplog.at_level("DEBUG", logger="agento.runner"):
        RemoteRunner("claude", ctx).execute(RunRequest(prompt="SECRET-PROMPT"))

    assert "SECRET" not in caplog.text and "job:1" in caplog.text


# --- A6: the runner has no database ------------------------------------------------------

def test_a_process_without_a_database_fails_fast(monkeypatch):
    from agento.framework import db

    monkeypatch.setattr(db, "DISABLED", True)
    monkeypatch.setattr("pymysql.connect", lambda *a, **k: pytest.fail("tried to connect"))

    with pytest.raises(RuntimeError, match="no database"):
        db.get_connection(object())


def test_a_timed_out_child_is_reaped_not_left_a_zombie():
    proc = subprocess.Popen(["sleep", "30"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    _, _, timed_out = subprocess_runner.collect(proc, None, timeout=0.2)
    assert timed_out and proc.returncode is not None
