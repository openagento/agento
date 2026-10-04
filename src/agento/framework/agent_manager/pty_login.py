"""Drive a vendor CLI login in a pseudo-terminal: read its output, answer its prompt.

Vendor-free. A harness module gives the command, the patterns, and how to read the
credential files the CLI writes into ``home`` (``start_web_login``).

The child gets a minimal environment, never ``os.environ``: the cron environment holds
``AGENTO_ENCRYPTION_KEY`` and ``MYSQL_PASSWORD`` (zero-trust.md, env inheritance). The CLI
dies with its worker: it is the session leader of the PTY, so the kernel sends it ``SIGHUP``
when the worker's master fd closes (exit or kill), and on Linux it also asks for
``SIGKILL`` when its parent dies (``PR_SET_PDEATHSIG``).
"""
from __future__ import annotations

import codecs
import ctypes
import fcntl
import os
import re
import select
import signal
import struct
import sys
import termios
import time
from collections.abc import Callable

from ..harness.protocols import AuthResult, LoginPrompt
from .errors import AuthenticationError

_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]|\r")
# The start of an escape sequence that the next read may complete: held back, not stripped.
_ANSI_PARTIAL = re.compile(r"\x1b(?:\][^\x07\x1b]*\x1b?|\[[0-?]*[ -/]*)?\Z")
_PARTIAL_MAX = 1024
_BUFFER_MAX = 64 * 1024
_PR_SET_PDEATHSIG = 1


class PtyProcess:
    """A child process on the slave side of a PTY. Output is kept, ANSI-stripped, bounded.

    A read can end inside a UTF-8 character or an escape sequence: both are held back until
    the next read completes them."""

    def __init__(self, pid: int, fd: int) -> None:
        self.pid = pid
        self._fd: int | None = fd
        self._text = ""
        self._tail = ""  # an escape sequence not yet complete
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._exit: int | None = None

    def _pump(self, timeout: float) -> bool:
        """Read what is ready within ``timeout``. False at end of output."""
        if self._fd is None:
            return False
        ready, _, _ = select.select([self._fd], [], [], max(0.0, timeout))
        if not ready:
            return True
        try:
            chunk = os.read(self._fd, 4096)
        except OSError:  # EIO: the child closed the slave side
            chunk = b""
        raw = self._tail + self._decoder.decode(chunk, final=not chunk)
        self._tail = ""
        if chunk and (p := _ANSI_PARTIAL.search(raw)) and len(p.group(0)) <= _PARTIAL_MAX:
            raw, self._tail = raw[:p.start()], p.group(0)
        self._text = (self._text + _ANSI.sub("", raw))[-_BUFFER_MAX:]
        if not chunk:
            os.close(self._fd)
            self._fd = None
            return False
        return True

    def read_until(self, pattern: re.Pattern, timeout: float) -> re.Match | None:
        """The first match of ``pattern`` in the output, waiting up to ``timeout`` s.

        A match that ends where the output read so far ends may still grow (a URL in two
        writes), so it counts only once more output follows it or the output ended. At the
        deadline the match is returned as it is."""
        deadline = time.monotonic() + timeout
        while True:
            m = pattern.search(self._text)
            if m and (m.end() < len(self._text) or self._fd is None):
                return m
            left = deadline - time.monotonic()
            if left <= 0 or not self._pump(min(left, 0.5)):
                return pattern.search(self._text)

    def write_line(self, text: str) -> None:
        if self._fd is not None:
            os.write(self._fd, text.encode() + b"\r")

    def exit_code(self) -> int | None:
        """The exit code once the child ended, else ``None``. Drains ready output first."""
        while self._fd is not None and select.select([self._fd], [], [], 0)[0]:
            self._pump(0)
        if self._exit is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self._exit = os.waitstatus_to_exitcode(status)
        return self._exit

    def kill(self) -> None:
        if self._exit is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
                _, status = os.waitpid(self.pid, 0)
                self._exit = os.waitstatus_to_exitcode(status)
            except (ProcessLookupError, ChildProcessError):
                pass
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def spawn(cmd: list[str], home: str) -> PtyProcess:
    """Start ``cmd`` in a new PTY with ``HOME=home`` and a minimal environment."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TERM": "dumb",
    }
    # Loaded before the fork: the child calls no dynamic loader.
    libc = ctypes.CDLL(None) if sys.platform == "linux" else None
    pid, fd = os.forkpty()
    if pid == 0:  # child
        try:
            if libc is not None:
                libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
            # Wide, so a long login URL is printed on one line and not wrapped.
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 4000, 0, 0))
            os.chdir(home)
            os.execvpe(cmd[0], cmd, env)
        finally:
            os._exit(127)
    return PtyProcess(pid, fd)


class PtyLogin:
    """An ``InteractiveLogin`` over a PTY process: ``parse`` reads the files the CLI wrote."""

    def __init__(self, proc: PtyProcess, prompt: LoginPrompt, parse: Callable[[], AuthResult]) -> None:
        self.proc = proc
        self.prompt = prompt
        self._parse = parse

    def submit_code(self, code: str) -> None:
        self.proc.write_line(code)

    def poll(self) -> AuthResult | None:
        code = self.proc.exit_code()
        if code is None:
            return None
        if code != 0:
            raise AuthenticationError(f"login CLI exited with code {code}")
        return self._parse()

    def close(self) -> None:
        self.proc.kill()
