"""Drive a vendor CLI login in a pseudo-terminal: read its output, answer its prompt.

Vendor-free. A harness module gives the command, the patterns, and how to read the
credential files the CLI writes into ``home`` (``start_web_login``).

The CLI runs in a runner (``runner.client.pty``), never in this process's container: the
worker's environment holds ``AGENTO_ENCRYPTION_KEY`` and ``MYSQL_PASSWORD``. The runner
gives it a minimal environment and kills it when this side closes the socket (exit, kill,
or a dead worker), so ``home`` must be on the shared /workspace (``client.shared_tmp``).
"""
from __future__ import annotations

import codecs
import os
import re
import select
import signal
import socket
import time
from collections.abc import Callable

from ..harness.protocols import AuthResult, LoginPrompt
from ..runner import client
from .errors import AuthenticationError

_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]|\r")
# The start of an escape sequence that the next read may complete: held back, not stripped.
_ANSI_PARTIAL = re.compile(r"\x1b(?:\][^\x07\x1b]*\x1b?|\[[0-?]*[ -/]*)?\Z")
_PARTIAL_MAX = 1024
_BUFFER_MAX = 64 * 1024


class PtyProcess:
    """A CLI on the slave side of a PTY in a runner, over a socket. Output is kept,
    ANSI-stripped, bounded. After the output the runner sends ``\\0<exit code>``.

    A read can end inside a UTF-8 character or an escape sequence: both are held back until
    the next read completes them."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock: socket.socket | None = sock
        self._fd: int | None = sock.fileno()
        self._text = ""
        self._tail = ""  # an escape sequence not yet complete
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._exit: int | None = None
        self._trailer: bytes | None = None  # bytes after the NUL: the exit code

    def _pump(self, timeout: float) -> bool:
        """Read what is ready within ``timeout``. False at end of output."""
        if self._fd is None:
            return False
        ready, _, _ = select.select([self._fd], [], [], max(0.0, timeout))
        if not ready:
            return True
        try:
            chunk = os.read(self._fd, 4096)
        except OSError:
            chunk = b""
        data = chunk
        if self._trailer is not None:
            self._trailer, data = self._trailer + chunk, b""
        elif b"\0" in chunk:
            data, _, self._trailer = chunk.partition(b"\0")
        raw = self._tail + self._decoder.decode(data, final=not chunk)
        self._tail = ""
        if chunk and (p := _ANSI_PARTIAL.search(raw)) and len(p.group(0)) <= _PARTIAL_MAX:
            raw, self._tail = raw[:p.start()], p.group(0)
        self._text = (self._text + _ANSI.sub("", raw))[-_BUFFER_MAX:]
        if not chunk:
            try:
                self._exit = int(self._trailer or b"")
            except ValueError:  # the runner died before the CLI ended
                self._exit = -1
            self._close()
            return False
        return True

    def _close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = self._fd = None

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
        if self._sock is not None:
            self._sock.sendall(text.encode() + b"\r")

    def exit_code(self) -> int | None:
        """The exit code once the CLI ended, else ``None``. Drains ready output first."""
        while self._fd is not None and select.select([self._fd], [], [], 0)[0]:
            self._pump(0)
        return self._exit

    def kill(self) -> None:
        """Closing the socket makes the runner SIGKILL the CLI."""
        if self._exit is None and self._sock is not None:
            self._exit = -signal.SIGKILL
        self._close()


def spawn(cmd: list[str], home: str) -> PtyProcess:
    """Start ``cmd`` in a new PTY in a runner, with ``HOME=home`` and a minimal environment."""
    return PtyProcess(client.pty(cmd, home))


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
