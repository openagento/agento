"""Test doubles for the root runner server (framework/runner/server.py).

The tests run as one ordinary user, so the privileged calls of ``runner/uids.py`` are fakes:
no uid change, and the "kill every process of the uid" step is a recorder (a real ``kill -1``
as the test user would kill the test run itself). ``signal_uid`` sends the signal to the
process groups the uid's runs reported, which is what ``kill -1`` reaches for them.
``thread_worker`` runs the real ``worker.serve`` in a thread, so a test can give the server a
fake in-process ``Runner``. The real uid steps are proved by the Linux root tests and the
live check (docs/architecture/runner.md#per-run-uid)."""
from __future__ import annotations

import contextlib
import json
import os
import threading
from collections import defaultdict
from pathlib import Path

from agento.framework.harness import HarnessRunContext
from agento.framework.runner import uids, worker


def run_dir(tmp_path: Path) -> Path:
    """A run dir: the server accepts only one under /workspace/artifacts or /workspace/.tmp."""
    path = tmp_path / "workspace" / "artifacts" / "ws" / "av" / "1"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_context(tmp_path: Path, tag: str = "job:1", **kw) -> HarnessRunContext:
    return HarnessRunContext(harness="claude", provider="anthropic", working_dir=str(run_dir(tmp_path)),
                             credential_required=False, tag=tag, timeout_seconds=30, **kw)


class FakeUids:
    def __init__(self, monkeypatch):
        self.calls: list[tuple] = []
        self.groups: dict[int, set[int]] = defaultdict(set)
        self.kill_ok = True
        self.real_run_dir = uids.run_dir  # a test that checks the rule puts it back
        monkeypatch.setattr(uids, "run_dir", self._run_dir)
        monkeypatch.setattr(uids, "as_uid", lambda uid: {})
        monkeypatch.setattr(uids, "drop_to", lambda uid: None)
        monkeypatch.setattr(uids, "own", self._own)
        monkeypatch.setattr(uids, "kill_all", self._kill_all)
        monkeypatch.setattr(uids, "signal_uid", self._signal_uid)

    @staticmethod
    def _run_dir(path):
        """Any directory: the tests' HOMEs live in tmp_path. The real rule (only under
        /workspace/artifacts or /workspace/.tmp) is tested on its own (unit/framework/runner/test_run_uid.py)."""
        real = os.path.realpath(path or "")
        if not path or not os.path.isdir(real):
            raise ValueError("the run dir is not a directory")
        return real

    def _own(self, path, uid, gid, *, for_run, retiring=None):
        self.calls.append(("own", path, uid, for_run))

    def _kill_all(self, uid):
        self.calls.append(("kill", uid))
        return self.kill_ok

    def _signal_uid(self, uid, sig):
        self.calls.append(("signal", uid, sig))
        for pid in self.groups[uid]:
            with contextlib.suppress(OSError):
                os.killpg(pid, sig)
        return True


class ThreadWorker:
    """A ``Popen``-like run worker: ``worker.serve`` in a thread, over two pipes."""

    def __init__(self, make_runner, uid: int, fake: FakeUids):
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        self.stdin, self.stdout = os.fdopen(w_in, "wb"), os.fdopen(r_out, "rb")
        self.uid = uid

        def run():
            with os.fdopen(r_in, "rb") as reader, os.fdopen(w_out, "wb") as out:
                send = worker.stdout_sender(out)

                def recorded(event):
                    if event.get("ev") == "pid":
                        fake.groups[uid].add(event["pid"])
                    with contextlib.suppress(OSError):
                        send(event)

                worker.serve(json.loads(reader.read()), recorded, make_runner)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def kill(self):
        pass  # a thread cannot be killed; the run's process group is (server._kill)

    def wait(self):
        self._thread.join(30)


def thread_worker(make_runner, fake: FakeUids):
    return lambda uid: ThreadWorker(make_runner, uid, fake)
