"""The per-run uid of the root runner server (WI-9b, docs/architecture/runner.md#per-run-uid):
the uid ranges and pool, the run-dir rule, the order of the cleanup, and the worker split.
The privileged steps are fakes here (tests/runner_fakes.py); the Linux root tests at the end
use the real ones."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agento.framework import workspace_paths
from agento.framework.harness import HarnessRunContext, RunRequest, RunResult
from agento.framework.runner import client, server, uids, worker
from agento.framework.runner.client import RemoteRunner

ROOT = os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0 and sys.platform == "linux"
ENTRYPOINT = Path(__file__).resolve().parents[4] / "src/agento/framework/docker/toolbox/entrypoint.sh"


@pytest.fixture(autouse=True)
def _no_usage_db(monkeypatch):
    monkeypatch.setattr(RemoteRunner, "_record_usage", lambda self, event: None)


def _rundir(tmp_path, name="1") -> Path:
    path = tmp_path / "workspace" / "artifacts" / "ws" / "av" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- ranges and the pool ---------------------------------------------------------------

def test_runner_ranges_never_overlap_whatever_their_max_procs():
    ranges = [uids.uid_range(i) for i in range(1, 9)]
    seen = set()
    for r in ranges:
        assert len(r) == uids.RUN_UID_SLOTS and not seen & set(r)
        seen |= set(r)
    assert min(seen) == uids.RUN_UID_BASE
    # Two runners with unequal bounds still take only from their own range.
    pools = [uids.UidPool(uids.uid_range(1)), uids.UidPool(uids.uid_range(2))]
    taken = [{pools[0].take() for _ in range(250)}, {pools[1].take() for _ in range(200)}]
    assert taken[0] <= set(uids.uid_range(1)) and taken[1] <= set(uids.uid_range(2))


def test_a_max_procs_over_the_range_refuses_to_start():
    with socket.socket(socket.AF_UNIX) as sock, pytest.raises(ValueError, match="at most 256"):
        server.Server(sock, max_procs=uids.RUN_UID_SLOTS + 1)


def test_the_pool_gives_unique_uids_under_concurrency_and_none_when_empty():
    pool = uids.UidPool(uids.uid_range(1))
    got: list[int | None] = []
    threads = [threading.Thread(target=lambda: got.append(pool.take())) for _ in range(250)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(got)) == 250 and None not in got
    rest = [pool.take() for _ in range(6)]
    assert None not in rest and pool.take() is None
    pool.give_back(rest[0])
    assert pool.take() == rest[0]


# --- the run-dir rule and the owner walk --------------------------------------------------

def test_run_dir_takes_only_a_dir_strictly_under_the_two_roots(tmp_path, monkeypatch):
    base = tmp_path / "workspace"
    monkeypatch.setattr(workspace_paths, "BASE_WORKSPACE_DIR", str(base))
    good = _rundir(tmp_path)
    tmp_home = base / ".tmp" / "h"
    tmp_home.mkdir(parents=True)
    outside = tmp_path / "etc"
    outside.mkdir()
    (base / "artifacts" / "escape").symlink_to(outside)

    assert uids.run_dir(str(good)) == os.path.realpath(good)
    assert uids.run_dir(str(tmp_home)) == os.path.realpath(tmp_home)
    for bad in (outside, base / "artifacts", base / ".tmp", base, base / "artifacts" / "escape",
                base / "artifacts" / "ws" / ".." / ".." / ".." / "etc"):
        with pytest.raises(ValueError):
            uids.run_dir(str(bad))
    (base / "artifacts" / "file").write_text("x")
    with pytest.raises(ValueError):
        uids.run_dir(str(base / "artifacts" / "file"))


def test_the_server_refuses_a_run_dir_outside_the_roots(start_runner, tmp_path, monkeypatch):
    srv = start_runner()
    monkeypatch.setattr(uids, "run_dir", srv.uids.real_run_dir)
    outside = tmp_path / "home"
    outside.mkdir()

    with pytest.raises(RuntimeError, match="not a directory under /workspace"):
        client.run(["true"], env={"HOME": str(outside)}, timeout=10)
    assert srv.uids.calls == []  # no uid taken, no owner change


def test_run_dir_refuses_a_symlinked_root_or_a_symlink_component(tmp_path, monkeypatch):
    """The roots are not resolved: a symlink anywhere on the path is refused (SEC-5)."""
    base = tmp_path / "workspace"
    monkeypatch.setattr(workspace_paths, "BASE_WORKSPACE_DIR", str(base))
    outside = tmp_path / "outside"
    (outside / "h").mkdir(parents=True)
    (base / "artifacts" / "real" / "av" / "1").mkdir(parents=True)
    (base / ".tmp").symlink_to(outside)                      # a redirected root
    (base / "artifacts" / "ws").symlink_to(base / "artifacts" / "real")  # a link inside

    for bad in (base / ".tmp" / "h", base / "artifacts" / "ws" / "av" / "1"):
        with pytest.raises(ValueError):
            uids.run_dir(str(bad))
    assert uids.run_dir(str(base / "artifacts" / "real" / "av" / "1")) == str(base / "artifacts" / "real" / "av" / "1")


def _tree(tmp_path, monkeypatch):
    """A run dir with a sub dir, a file and two planted links, and an outside dir."""
    base = tmp_path / "workspace"
    monkeypatch.setattr(workspace_paths, "BASE_WORKSPACE_DIR", str(base))
    top = _rundir(tmp_path)
    (top / "sub").mkdir()
    (top / "sub" / "f").write_text("x")
    (top / "f").write_text("x")
    os.chmod(top / "f", 0o600)
    outside = tmp_path / "victim"
    outside.mkdir()
    (outside / "secret").write_text("x")
    (top / "link").symlink_to(outside)
    (top / "flink").symlink_to(outside / "secret")
    return top, outside


def _record_changes(monkeypatch):
    """Real owner changes (to our own uid), with the inode each call reached."""
    seen: dict[str, list] = {"fchown": [], "fchmod": [], "chown": []}
    real = {n: getattr(os, n) for n in seen}

    def fchown(fd, uid, gid):
        seen["fchown"].append(os.fstat(fd).st_ino)
        real["fchown"](fd, uid, gid)

    def fchmod(fd, mode):
        seen["fchmod"].append(os.fstat(fd).st_ino)
        real["fchmod"](fd, mode)

    def chown(name, uid, gid, *, dir_fd=None, follow_symlinks=True):
        assert dir_fd is not None and follow_symlinks is False, "a chown by path or through a link"
        seen["chown"].append(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_ino)
        real["chown"](name, uid, gid, dir_fd=dir_fd, follow_symlinks=False)

    for name, fn in (("fchown", fchown), ("fchmod", fchmod), ("chown", chown)):
        monkeypatch.setattr(os, name, fn)
    monkeypatch.setattr(os, "lchown", lambda *a: pytest.fail("lchown by path"))
    return seen


def _inodes(*paths):
    return {os.lstat(p).st_ino for p in paths}


def test_own_does_not_follow_a_planted_symlink_and_sets_dirs_2770(tmp_path, monkeypatch):
    top, outside = _tree(tmp_path, monkeypatch)
    seen = _record_changes(monkeypatch)

    uids.own(str(top), os.getuid(), os.getgid(), for_run=True)

    assert seen["chown"] == []  # a symlink keeps its owner: no change by name
    assert set(seen["fchown"]) == _inodes(top, top / "sub", top / "sub" / "f", top / "f")
    assert set(seen["fchmod"]) == _inodes(top, top / "sub")
    assert oct((top / "sub").stat().st_mode & 0o7777) == oct(uids.DIR_MODE)
    assert oct((top / "f").stat().st_mode & 0o777) == oct(0o600)  # file modes stay
    assert oct(outside.stat().st_mode & 0o7777) != oct(uids.DIR_MODE)


def test_a_hard_link_to_an_outside_file_keeps_that_file_unchanged(tmp_path, monkeypatch):
    """An ``agent`` process may link a file it owns into the run dir (protected_hardlinks
    allows it). The walk must not give that inode to the run (SEC-5)."""
    top, outside = _tree(tmp_path, monkeypatch)
    os.link(outside / "secret", top / "sub" / "planted")
    seen = _record_changes(monkeypatch)

    uids.own(str(top), os.getuid(), os.getgid(), for_run=True)

    assert not _inodes(outside / "secret") & set(seen["chown"] + seen["fchown"])
    assert _inodes(top / "f", top / "sub" / "f") <= set(seen["fchown"])  # the rest is still changed


RETIRING = uids.RUN_UID_BASE + 7


@pytest.mark.parametrize("owner, changed", [
    ((RETIRING, 1000), True),               # the retiring run's uid
    ((1000, RETIRING), True),               # its gid (a toolbox file in the setgid dir)
    ((1000, RETIRING + 1), False),          # another live run's gid
])
def test_a_hard_linked_inode_goes_back_only_when_the_retiring_run_holds_it(
        tmp_path, monkeypatch, owner, changed):
    """A run links its own file outside its dir: on the way back that inode is changed anyway,
    so the uid keeps nothing for its next run (SEC-9). A linked inode held only by another
    live run keeps its owner (SEC-5). Only the owner ``fstat`` reports for that inode is set
    (a non-root test cannot make it); the open and the change are real."""
    top, outside = _tree(tmp_path, monkeypatch)
    alias = outside / "alias"
    os.link(top / "f", alias)
    ino, real_fstat = os.lstat(alias).st_ino, os.fstat

    def fstat(fd):
        st = real_fstat(fd)
        if st.st_ino != ino:
            return st
        fields = list(st[:10])
        fields[4:6] = owner
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", fstat)
    seen = _record_changes(monkeypatch)

    uids.own(str(top), os.getuid(), os.getgid(), for_run=False, retiring=RETIRING)
    assert ({ino} <= set(seen["fchown"])) is changed

    seen["fchown"].clear()
    uids.own(str(top), os.getuid(), os.getgid(), for_run=True)
    assert ino not in seen["fchown"]  # on the way in it is always skipped


def test_a_path_swapped_after_the_check_does_not_move_the_walk(tmp_path, monkeypatch):
    """The check passed; then a sub dir becomes a link to outside. The walk never reaches it."""
    top, outside = _tree(tmp_path, monkeypatch)
    uids.run_dir(str(top))
    os.rename(top / "sub", tmp_path / "moved")
    (top / "sub").symlink_to(outside)
    seen = _record_changes(monkeypatch)
    mode = outside.stat().st_mode

    uids.own(str(top), os.getuid(), os.getgid(), for_run=True)

    assert not _inodes(outside, outside / "secret") & set(seen["chown"] + seen["fchown"] + seen["fchmod"])
    assert outside.stat().st_mode == mode


def test_a_swap_during_the_walk_does_not_move_it(tmp_path, monkeypatch):
    """The sub dir is listed as a dir, then swapped for a link before the walk opens it."""
    top, outside = _tree(tmp_path, monkeypatch)
    seen = _record_changes(monkeypatch)
    recorded_fchmod = os.fchmod

    def fchmod(fd, mode):
        if not seen["fchmod"]:  # the top dir: listed already, its sub dirs not yet opened
            os.rename(top / "sub", tmp_path / "moved")
            (top / "sub").symlink_to(outside)
        recorded_fchmod(fd, mode)

    monkeypatch.setattr(os, "fchmod", fchmod)
    mode = outside.stat().st_mode

    uids.own(str(top), os.getuid(), os.getgid(), for_run=True)

    assert (top / "sub").is_symlink()  # the swap did happen mid-walk

    assert not _inodes(outside, outside / "secret") & set(seen["chown"] + seen["fchown"] + seen["fchmod"])
    assert outside.stat().st_mode == mode


def test_a_run_dir_swapped_for_a_link_after_the_check_is_refused(tmp_path, monkeypatch):
    top, outside = _tree(tmp_path, monkeypatch)
    real = uids.run_dir(str(top))
    os.rename(top, tmp_path / "moved")
    top.symlink_to(outside)
    seen = _record_changes(monkeypatch)
    with pytest.raises(ValueError):
        uids.own(real, os.getuid(), os.getgid(), for_run=True)
    assert seen == {"fchown": [], "fchmod": [], "chown": []}


def test_as_uid_gives_the_uid_its_own_group_only():
    assert uids.as_uid(20001) == {"user": 20001, "group": 20001, "extra_groups": [], "umask": 0o002}


def test_the_worker_is_spawned_as_the_run_uid(monkeypatch):
    seen = {}
    monkeypatch.setattr(server.subprocess, "Popen", lambda argv, **kw: seen.update(argv=argv, **kw))
    server.spawn_worker(20007)
    assert seen["argv"] == server.WORKER
    assert (seen["user"], seen["group"], seen["extra_groups"]) == (20007, 20007, [])


def test_processes_of_reads_the_uid_line_and_skips_zombies(tmp_path):
    for pid, uid_line, state in ((10, "20001 20001 20001 20001", "S"), (11, "0 0 20001 0", "R"),
                                 (12, "20001 20001 20001 20001", "Z"), (13, "501 501 501 501", "S")):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "status").write_text(f"Name:\tx\nState:\t{state} (x)\nUid:\t{uid_line}\n")
    (tmp_path / "14").mkdir()  # the process ended between the listing and the read
    assert sorted(uids.processes_of(20001, str(tmp_path))) == [10, 11]


def test_a_proc_scan_that_did_not_finish_is_never_clean(tmp_path, monkeypatch):
    """Only a vanished process is "gone"; any other read error is a survivor (SEC-9)."""
    (tmp_path / "15" / "status").mkdir(parents=True)  # a read error that is not "gone"
    with pytest.raises(OSError):
        uids.processes_of(20001, str(tmp_path))
    with pytest.raises(FileNotFoundError):
        uids.processes_of(20001, str(tmp_path / "no-proc"))

    monkeypatch.setattr(uids, "signal_uid", lambda uid, sig: True)
    monkeypatch.setattr(uids, "KILL_PAUSE_S", 0)

    def unreadable(uid):
        raise PermissionError("status")

    monkeypatch.setattr(uids, "processes_of", unreadable)
    assert uids.kill_all(20001) is False


def test_kill_all_gives_up_after_its_bound(monkeypatch):
    monkeypatch.setattr(uids, "signal_uid", lambda uid, sig: True)
    monkeypatch.setattr(uids, "KILL_PAUSE_S", 0)
    monkeypatch.setattr(uids, "processes_of", lambda uid: [1])
    assert uids.kill_all(20001) is False
    monkeypatch.setattr(uids, "processes_of", lambda uid: [])
    assert uids.kill_all(20001) is True


# --- the cleanup order ------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["true"], ["/no/such/cli"]])
def test_a_uid_is_free_only_after_the_kill_and_the_chown_back(start_runner, tmp_path, argv):
    srv = start_runner()
    home = str(_rundir(tmp_path))
    with pytest.raises(FileNotFoundError) if argv[0].startswith("/no") else contextlib.nullcontext():
        client.run(argv, env={"HOME": home}, timeout=10)

    path, uid = srv.uids.calls[0][1], srv.uids.calls[0][2]
    assert srv.uids.calls == [("own", path, uid, True), ("kill", uid), ("own", path, os.getuid(), False)]
    assert path == os.path.realpath(home)
    assert uid in srv._uids._free


def test_a_uid_with_survivors_is_kept_out_of_the_pool_and_its_dir_is_not_walked(start_runner, tmp_path):
    """A survivor could swap an entry during the walk; reclaim does it at the next start."""
    state = tmp_path / "live"
    state.mkdir()
    srv = start_runner(state_dir=str(state))
    srv.uids.kill_ok = False
    with pytest.raises(RuntimeError, match="runner cleanup incomplete"):
        client.run(["true"], env={"HOME": str(_rundir(tmp_path))}, timeout=10)
    uid = srv.uids.calls[0][2]
    assert [c[0] for c in srv.uids.calls] == ["own", "kill"]
    assert os.listdir(state) == [str(uid)]
    assert uid not in srv._uids._free and len(srv._uids._free) == uids.RUN_UID_SLOTS - 1


def test_a_failed_give_back_is_an_error_and_keeps_the_uid_and_note(start_runner, tmp_path, monkeypatch):
    state = tmp_path / "live"
    state.mkdir()
    srv = start_runner(state_dir=str(state))
    fake_own = uids.own

    def own(path, uid, gid, *, for_run, retiring=None):
        fake_own(path, uid, gid, for_run=for_run, retiring=retiring)
        if not for_run:
            raise PermissionError("chown")

    monkeypatch.setattr(uids, "own", own)
    with pytest.raises(RuntimeError, match="runner cleanup incomplete"):
        client.run(["true"], env={"HOME": str(_rundir(tmp_path))}, timeout=10)
    uid = srv.uids.calls[0][2]
    assert os.listdir(state) == [str(uid)] and uid not in srv._uids._free


def test_a_pty_with_survivors_never_sends_an_exit_code(start_runner, tmp_path):
    """No ``\\0<code>`` trailer: the login reads exit code -1, a failure (pty_login.py)."""
    srv = start_runner()
    srv.uids.kill_ok = False
    sock = client.pty(["true"], str(_rundir(tmp_path)))
    data = b""
    while chunk := sock.recv(4096):
        data += chunk
    sock.close()
    assert b"\0" not in data and b"runner cleanup incomplete" in data


def test_a_run_dir_that_cannot_be_given_to_the_run_is_an_error(start_runner, tmp_path, monkeypatch):
    srv = start_runner()
    fake_own = uids.own

    def own(path, uid, gid, *, for_run, retiring=None):
        fake_own(path, uid, gid, for_run=for_run, retiring=retiring)
        if for_run:
            raise ValueError("the run dir has a symlink on its path")

    monkeypatch.setattr(uids, "own", own)
    with pytest.raises(RuntimeError, match="could not be given to the run"):
        client.run(["true"], env={"HOME": str(_rundir(tmp_path))}, timeout=10)
    uid = srv.uids.calls[0][2]
    assert [c[0] for c in srv.uids.calls] == ["own", "kill", "own"] and uid in srv._uids._free


def test_an_execute_with_survivors_ends_in_an_error_and_stays_alive(start_runner, tmp_path):
    """The terminal frame says the cleanup is incomplete, and ``alive`` does not say "dead"
    while a process of the run may still go on (no resume of that session)."""
    srv = start_runner(make_runner=lambda h, ctx, dry_run: _Ok())
    srv.uids.kill_ok = False
    runner = RemoteRunner("claude", _ctx(tmp_path, tag="job:9"))
    with pytest.raises(RuntimeError, match="runner cleanup incomplete"):
        runner.execute(RunRequest(prompt="p"))
    assert srv._alive(None, {"tag": "job:9"}, None)["state"] == "alive"


def _ctx(tmp_path, tag="job:1"):
    return HarnessRunContext(harness="claude", provider="anthropic", working_dir=str(_rundir(tmp_path)), home_dir=str(_rundir(tmp_path)),
                             credential_required=False, tag=tag, timeout_seconds=30)


class _Ok:
    def observe(self, **kw):
        pass

    def execute(self, request):
        return RunResult(raw_output="ok")


class _Boom(_Ok):
    def execute(self, request):
        raise RuntimeError("boom")


@pytest.mark.parametrize("op", ["execute-result", "execute-error", "run", "pty"])
def test_the_terminal_frame_comes_only_after_the_chown_back(start_runner, tmp_path, monkeypatch, op):
    srv = start_runner(make_runner=lambda h, ctx, dry_run: (_Boom if op == "execute-error" else _Ok)())
    order = []
    fake_own = uids.own

    def own(path, uid, gid, *, for_run, retiring=None):
        if not for_run:
            time.sleep(0.2)  # a slow walk: the client must still wait for it
            order.append("chown-back")
        fake_own(path, uid, gid, for_run=for_run, retiring=retiring)

    monkeypatch.setattr(uids, "own", own)
    home = str(_rundir(tmp_path))
    if op.startswith("execute"):
        with contextlib.suppress(RuntimeError):
            RemoteRunner("claude", _ctx(tmp_path)).execute(RunRequest(prompt="p"))
    elif op == "run":
        client.run(["true"], env={"HOME": home}, timeout=10)
    else:
        sock = client.pty(["true"], home)
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
        sock.close()
        assert data.endswith(b"\x000")
    order.append("terminal")

    assert order == ["chown-back", "terminal"]
    assert [c[0] for c in srv.uids.calls] == ["own", "kill", "own"]


def test_the_pty_child_drops_to_the_run_uid(start_runner, tmp_path, monkeypatch):
    start_runner()
    mark = tmp_path / "uid"
    monkeypatch.setattr(uids, "drop_to", lambda uid: mark.write_text(str(uid)))
    sock = client.pty(["true"], str(_rundir(tmp_path)))
    while sock.recv(4096):
        pass
    sock.close()
    assert int(mark.read_text()) in uids.uid_range(1)


def test_run_starts_the_cli_as_the_run_uid(start_runner, tmp_path, monkeypatch):
    start_runner()
    asked = []
    monkeypatch.setattr(uids, "as_uid", lambda uid: asked.append(uid) or {})
    client.run(["true"], env={"HOME": str(_rundir(tmp_path))}, timeout=10)
    assert len(asked) == 1 and asked[0] in uids.uid_range(1)


class _Sleep(_Ok):
    def observe(self, *, on_pid=None, **kw):
        self.on_pid = on_pid

    def execute(self, request):
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.on_pid(proc.pid)
        proc.wait()
        return RunResult(raw_output=f"rc={proc.returncode}")


def test_signal_goes_to_every_process_of_the_runs_uid(start_runner, tmp_path):
    srv = start_runner(make_runner=lambda h, ctx, dry_run: _Sleep())
    runner = RemoteRunner("claude", _ctx(tmp_path, tag="job:7"))
    out = {}
    pids = []
    runner.observe(on_pid=pids.append)
    t = threading.Thread(target=lambda: out.update(r=runner.execute(RunRequest(prompt="p"))))
    t.start()
    assert _wait(lambda: pids)

    assert client.signal(runner.runner_ref, "job:7", signal.SIGTERM) is True
    t.join(10)

    uid = srv.uids.calls[0][2]
    assert ("signal", uid, signal.SIGTERM) in srv.uids.calls
    assert out["r"].raw_output == f"rc={-signal.SIGTERM}"


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --- the worker split -------------------------------------------------------------------

def test_the_server_process_loads_no_harness_or_module_code():
    code = ("import socket, sys\n"
            "from agento.framework.runner import server\n"
            "from agento.framework.harness import list_harnesses\n"
            "server.Server(socket.socket(socket.AF_UNIX))\n"
            "mods = [m for m in sys.modules if m.startswith('agento.modules')]\n"
            "print(mods, list_harnesses())\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[] []"


def test_the_worker_relays_the_events_of_an_http_style_runner():
    """A runner with no CLI (an HTTP harness) reports through the same seams."""
    class _Http:
        def observe(self, *, on_pid=None, on_session_id=None, on_usage=None):
            self.sid, self.usage = on_session_id, on_usage

        def execute(self, request):
            self.sid("s-9")
            self.usage(RunResult(raw_output="", input_tokens=1, output_tokens=2, model="m"))
            return RunResult(raw_output="x" * 3 * worker.wire.CHUNK_CHARS, session_id="s-9")

    sent = []
    req = {"context": json.loads(worker.wire.encode(_ctx_plain())), "request": {"prompt": "p"}}
    worker.serve(req, sent.append, lambda h, ctx, dry_run: _Http())

    kinds = [e["ev"] for e in sent]
    assert kinds[:2] == ["session", "usage"] and kinds[-1] == "result"
    assert "".join(e["data"] for e in sent if e["ev"] == "output") == "x" * 3 * worker.wire.CHUNK_CHARS


def _ctx_plain():
    return HarnessRunContext(harness="http", provider="p", credential_required=False, tag="job:2")


# --- the toolbox entrypoint ---------------------------------------------------------------

@pytest.mark.parametrize("count", [1, 2])
def test_the_toolbox_gets_every_run_gid_and_umask_002(tmp_path, count):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {"id": 'case "$1" in -u) echo 501;; -g) echo 501;; esac',
                       "getent": "echo agent:x:501:501::/home/agent:/bin/sh",
                       "setpriv": 'echo "$@"; umask; echo "$HOME"'}.items():
        (bindir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bindir / name).chmod(0o755)
    seq = shutil.which("seq")
    script = ENTRYPOINT.read_text()
    tail = script[script.index("RUN_UID_BASE="):]
    out = subprocess.run(["sh", "-c", tail, "entrypoint", "node", "server.js"], capture_output=True, text=True,
                         env={"PATH": f"{bindir}:{os.path.dirname(seq)}:/usr/bin:/bin",
                              "AGENTO_RUNNER_COUNT": str(count)}, check=True).stdout.split("\n")

    args = out[0].split()
    groups = args[args.index("--groups") + 1].split(",")
    assert [int(g) for g in groups] == list(range(uids.RUN_UID_BASE, uids.RUN_UID_BASE + count * uids.RUN_UID_SLOTS))
    assert args[-2:] == ["node", "server.js"] and out[1] == "0002" and out[2] == "/home/agent"


def test_the_toolbox_entrypoint_uses_the_same_uid_constants():
    text = ENTRYPOINT.read_text()
    assert f"RUN_UID_BASE={uids.RUN_UID_BASE}\n" in text and f"RUN_UID_SLOTS={uids.RUN_UID_SLOTS}\n" in text


# --- Linux root: the real steps -------------------------------------------------------------

@pytest.mark.skipif(not ROOT, reason="needs root on Linux (the runner container)")
def test_real_own_kill_and_chown_back(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_paths, "BASE_WORKSPACE_DIR", str(tmp_path / "workspace"))
    top = _rundir(tmp_path)
    uid = uids.uid_range(8)[0]
    uids.own(str(top), uid, uid, for_run=True)
    assert (top.stat().st_uid, top.stat().st_mode & 0o7777) == (uid, uids.DIR_MODE)
    subprocess.Popen(["setsid", "sleep", "60"], **uids.as_uid(uid), stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    assert _wait(lambda: uids.processes_of(uid))
    assert uids.kill_all(uid) and uids.processes_of(uid) == []
    uids.own(str(top), 0, 0, for_run=False)
    assert top.stat().st_uid == 0


def test_a_run_dir_left_by_a_dead_server_is_given_back_at_start(start_runner, tmp_path, monkeypatch):
    """The note is written before the walk and removed after the chown-back; a note that is
    left (the server died) gives the dir back at the next start, through the run-dir rule."""
    state = tmp_path / "live"
    state.mkdir()
    srv = start_runner(state_dir=str(state))
    seen = []
    fake_own = uids.own
    monkeypatch.setattr(uids, "own", lambda *a, **k: (seen.append(sorted(os.listdir(state))), fake_own(*a, **k)))
    home = _rundir(tmp_path)
    client.run(["true"], env={"HOME": str(home)}, timeout=10)
    uid = srv.uids.calls[0][2]
    assert seen == [[str(uid)], [str(uid)]] and os.listdir(state) == []

    (state / "20009").write_text(str(home))
    (state / "20010").write_text("/etc")  # outside the roots: never walked, the uid kept out
    srv.uids.calls.clear()
    monkeypatch.setattr(uids, "run_dir", srv.uids.real_run_dir)
    assert uids.reclaim(str(state), 7, 7) == {20010}
    assert srv.uids.calls == [("own", os.path.realpath(home), 7, False)]
    assert os.listdir(state) == ["20010"]  # the evidence stays for the operator


def test_a_failed_reclaim_keeps_the_note_and_the_uid_out_of_the_pool(start_runner, tmp_path, monkeypatch):
    state = tmp_path / "live"
    state.mkdir()
    first = uids.uid_range(1)[3]
    (state / str(first)).write_text(str(_rundir(tmp_path)))

    def own(*a, **k):
        raise PermissionError("chown")

    monkeypatch.setattr(uids, "own", own)
    srv = start_runner(state_dir=str(state))
    assert os.listdir(state) == [str(first)]
    assert first not in srv._uids._free and len(srv._uids._free) == uids.RUN_UID_SLOTS - 1


def test_the_cron_entrypoint_heal_never_touches_a_run_uid():
    text = (ENTRYPOINT.parents[1] / "cron" / "entrypoint.sh").read_text()
    assert "chown -R agent:agent /workspace/artifacts" not in text
    assert f"find /workspace/artifacts -uid -{uids.RUN_UID_BASE} " in text
