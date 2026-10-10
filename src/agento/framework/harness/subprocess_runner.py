"""SubprocessRunner — executes a harness CLI for one run.

Formerly ``agent_manager.runner.TokenRunner``. Two things changed beyond the move:

- **Command building left.** Flags come from the harness's :class:`CommandBuilder`, so
  headless and interactive can no longer drift apart.
- **Credential selection left.** The caller claims the credential once and puts it on
  the :class:`HarnessRunContext`; this runner only consumes it. With two resolvers, one
  run could build its command from one credential and execute against another.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import signal
import subprocess
import threading
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from pathlib import Path

from ..ssh_identity import without_run_owned_ssh_env
from ..ssh_prelude import wrap_with_ssh_prelude
from .protocols import CommandBuilder
from .runtime import HarnessRunContext, RunRequest, RunResult


def harness_base_env() -> dict[str, str]:
    """The environment a harness CLI starts from: the runner's own env (it has no store,
    ``test_runner_boundary.py``) without the run-owned SSH names."""
    return without_run_owned_ssh_env(dict(os.environ))


# The retained tail of each stream. A run's output is kept in memory until it ends; the
# oldest lines go first, so `agent_output` is a tail, as on the timeout path (SCL-1).
MAX_RETAINED = 4 << 20


def collect(proc: subprocess.Popen, stdin_payload: str | None, timeout: float | None,
            on_line: Callable[[str, bool], None] | None = None) -> tuple[str, str, bool]:
    """Feed, drain and wait for ``proc`` (text mode, ``start_new_session=True``), then kill
    its process group, so no descendant outlives the run or holds its pipes. Returns the
    last ``MAX_RETAINED`` chars of stdout and stderr, and whether it timed out. A read is
    at most ``MAX_RETAINED`` chars: a longer line reaches ``on_line(line, from_stdout)``
    in pieces.

    The payload is written from its OWN thread: a harness may read stdin only after its
    startup work, so a payload larger than the pipe buffer would block the wait below."""
    def _write_stdin(stream) -> None:
        # The child may die before reading it all (a failed extension load): the
        # normal rc!=0 path then reports the real error instead of this symptom.
        with contextlib.suppress(BrokenPipeError, ValueError, OSError):
            stream.write(stdin_payload)
        with contextlib.suppress(BrokenPipeError, ValueError, OSError):
            stream.close()

    def _drain(stream, tail: deque[str], from_stdout: bool) -> None:
        kept = 0
        for line in iter(lambda: stream.readline(MAX_RETAINED), ""):
            tail.append(line)
            kept += len(line)
            while kept > MAX_RETAINED:
                kept -= len(tail.popleft())
            if on_line:
                on_line(line, from_stdout)

    tails: tuple[deque[str], deque[str]] = (deque(), deque())
    threads = [threading.Thread(target=_drain, args=(proc.stdout, tails[0], True), daemon=True),
               threading.Thread(target=_drain, args=(proc.stderr, tails[1], False), daemon=True)]
    if stdin_payload is not None:
        threads.append(threading.Thread(target=_write_stdin, args=(proc.stdin,), daemon=True))
    for t in threads:
        t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)  # reap the leader the kill just ended: no zombie per timeout
    for t in threads:
        t.join(timeout=5)
    return "".join(tails[0]), "".join(tails[1]), timed_out


# A session id goes into a glob: no `*`, `?`, `[`, `/` or `..`.
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def move_session_into(store: Path, bucket: str, name_glob: str) -> bool:
    """Move the newest ``<store>/*/<name_glob>`` into ``<store>/<bucket>``.

    For a CLI that files sessions under a slug of its cwd. Moved, not copied: one file per
    session, so a transcript reader finds one match. ``False`` when no such file exists.
    """
    target = store / bucket
    if any(target.glob(name_glob)):
        return True
    found = sorted(store.glob(f"*/{name_glob}"), key=lambda p: p.stat().st_mtime)
    if not found:
        return False
    target.mkdir(parents=True, exist_ok=True)
    found[-1].rename(target / found[-1].name)
    return True


class SubprocessRunner(ABC):
    """Runs one harness CLI invocation. Subclasses only parse output."""

    def __init__(
        self,
        *,
        context: HarnessRunContext,
        command_builder: CommandBuilder,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
    ):
        self.context = context
        self.command_builder = command_builder
        self.logger = logger or logging.getLogger(__name__)
        self.dry_run = dry_run
        # Set through `observe()` — see the Runner protocol for why that is a method
        # rather than two attributes the caller assigns.
        self.pid_callback: Callable[[int], None] | None = None
        self.session_id_callback: Callable[[str], None] | None = None
        # One line of the harness's stdout, raw. The callback maps and enqueues it; it does
        # NO DB work, because this runs on the drain thread and a query here stalls the run.
        self.line_callback: Callable[[str], None] | None = None
        # The parsed result, before the exit code is judged: usage is recorded where the
        # DB is (the worker), also for a run that exited non-zero.
        self.usage_callback: Callable[[RunResult], None] | None = None
        # Prompt-free rendering of the current command, for logs AND exception strings
        # (a TimeoutExpired's `cmd` ends up in job.error_message).
        self._log_cmd: str | None = None

    # -- abstract hooks -------------------------------------------------------

    @abstractmethod
    def _parse_output(self, raw: str) -> RunResult:
        """Parse raw CLI stdout into a RunResult."""
        ...

    @abstractmethod
    def _credential_env(self, credential: object | None) -> dict[str, str]:
        """Env-var overrides derived from the credential payload ({} when none)."""
        ...

    def _extract_raw(self, proc: subprocess.CompletedProcess) -> str:
        """Raw string to hand to ``_parse_output``. Default: stdout, else stderr."""
        return proc.stdout or proc.stderr

    def _try_parse_session_id(self, line: str) -> str | None:
        """Extract a session id from one output line, incrementally during execution."""
        return None

    # -- public entry point ---------------------------------------------------

    def prepare_resume(self, session_id: str) -> bool:
        """Make ``session_id`` resumable by THIS run. ``False``: the session is gone.

        An optional hook, not part of the ``Runner`` protocol: a caller treats a runner
        without it as "found". A conversation's next turn is a new job in a new working
        directory, and some CLIs file a session under a slug of the directory it was made
        in; a harness runner overrides this to move it here. On ``False`` the caller starts
        a fresh session and sends the whole history. Default: the CLI finds a session
        wherever it was made (codex keys its store by id alone).
        """
        return True

    def execute(self, request: RunRequest) -> RunResult:
        """Run headlessly. ``request.session_id`` set resumes that session."""
        if self.dry_run:
            self.logger.info(
                "[DRY RUN] DISABLE_LLM is set, skipping %s run.", self.context.harness
            )
            return RunResult(raw_output="[DRY RUN] skipped")

        ctx = self.context
        # Headless path: a required-but-absent credential is a hard error before the
        # process starts, so a job fails with a clear message instead of burning a
        # session. The interactive `/login` flow goes through CommandBuilder.interactive().
        if ctx.credential_required and ctx.credential is None:
            raise RuntimeError(
                f"No healthy credential for harness={ctx.harness} provider={ctx.provider}. "
                f"Register one: bin/agento credential:register <scope> <label>"
            )

        # extra_env last: GIT_AUTHOR_*/GIT_COMMITTER_* must override inherited git env.
        # The SSH names are STRIPPED from the inherited base first: a run with no identity
        # contributes none of them, so a merge would leave whatever the consumer process
        # inherited in place and the spawn prelude would load a key this run was never
        # granted. See ssh_identity.RUN_OWNED_SSH_ENV_VARS.
        env = {
            **harness_base_env(),
            **self._credential_env(ctx.credential),
            **ctx.extra_env,
        }
        cmd = self.command_builder.headless(ctx, request)
        return self._execute_and_parse(cmd, env, request)

    def _cmd_metadata(self, cmd: list[str], request: RunRequest) -> str:
        """An ALLOWLISTED description of the command — never the argv itself.

        Four review rounds were spent trying to sanitize plugin-returned argv: first the
        prompt element, then any argument containing it. Both failed, because the premise was
        wrong. ``cmd`` comes from a third-party ``CommandBuilder``, so it can

        * transform the prompt (JSON-escape a newline) past any substring match, and
        * carry secrets of its own (``--api-key=sk-…``) that the framework cannot enumerate.

        There is no redaction that is sound against argv the framework does not control. So
        nothing derived from ``cmd`` is logged except its length and its executable — which is
        ``cmd[0]``, chosen by the harness module, not by any prompt or credential.
        """
        model = request.model or self.context.model
        return (
            f"bin={cmd[0] if cmd else '?'} argv={len(cmd)} "
            f"prompt_len={len(request.prompt or '')} "
            f"model={'set' if model else 'default'} "
            f"resume={'yes' if request.session_id else 'no'}"
        )

    def observe(
        self,
        *,
        on_pid=None,
        on_session_id=None,
        on_line=None,
        on_usage=None,
    ) -> None:
        """Register the progress callbacks (``Runner`` protocol)."""
        if on_pid is not None:
            self.pid_callback = on_pid
        if on_session_id is not None:
            self.session_id_callback = on_session_id
        if on_line is not None:
            self.line_callback = on_line
        if on_usage is not None:
            self.usage_callback = on_usage

    @staticmethod
    def _failure_output(stdout: str, stderr: str) -> str:
        """What to persist to ``job.output`` for a failed run.

        Kept separate from the parser's input (which is stdout only — mixing streams would
        corrupt NDJSON parsing). A harness that writes its diagnostics to stderr and nothing
        to stdout would otherwise persist an EMPTY output and leave the operator with no
        record at all, which is the failure mode this whole path exists to prevent.
        """
        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"--- stderr ---\n{stderr}" if stdout else stderr)
        return "\n".join(parts)

    # -- process execution ----------------------------------------------------

    def _execute_process(
        self, cmd: list[str], env: dict, stdin_payload: str | None = None
    ) -> subprocess.CompletedProcess:
        """Execute a subprocess with incremental output reading (``collect``), so the
        session id is reported at once and partial output survives a timeout."""
        if self.context.home_dir is not None:
            env = {**env, "HOME": self.context.home_dir}
            spawn_cmd = wrap_with_ssh_prelude(cmd)
        else:
            spawn_cmd = cmd

        proc = subprocess.Popen(
            spawn_cmd,
            stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.context.working_dir,
            env=env,
            # Its own process group: the runner signals and kills the whole run (pause,
            # disconnect), not only the shell prelude.
            start_new_session=True,
        )

        if self.pid_callback:
            try:
                self.pid_callback(proc.pid)
            except Exception:
                self.logger.warning(f"PID callback failed for pid={proc.pid}")

        session_id_found: str | None = None

        def _on_line(line: str, from_stdout: bool) -> None:
            nonlocal session_id_found
            # Only stdout is streamed: the event stream is there, and stderr is diagnostics.
            if from_stdout and self.line_callback:
                # Caught and logged like `session_id_callback`: a streaming seam must
                # never be able to fail a run.
                try:
                    self.line_callback(line)
                except Exception:
                    self.logger.warning("line_callback failed")
            if session_id_found is None:
                sid = self._try_parse_session_id(line)
                if sid:
                    session_id_found = sid
                    if self.session_id_callback:
                        try:
                            self.session_id_callback(sid)
                        except Exception:
                            self.logger.warning(f"session_id_callback failed for sid={sid}")

        stdout, stderr, timed_out = collect(proc, stdin_payload, self.context.timeout_seconds,
                                            _on_line)

        if timed_out:
            session_id = session_id_found or self._extract_session_id_from_partial(stdout, stderr)
            # Redacted cmd: TimeoutExpired.__str__ renders it, and this exception's text
            # is persisted to job.error_message where an operator (and any log shipper)
            # will read it. `output`/`stderr` are the agent's own output, which the
            # operator needs to diagnose the timeout.
            exc = subprocess.TimeoutExpired(
                cmd=self._log_cmd or (cmd[0] if cmd else ""),
                timeout=self.context.timeout_seconds,
                output=stdout,
                stderr=stderr,
            )
            exc.session_id = session_id  # type: ignore[attr-defined]
            # Same single-destination rule as the rc!=0 path: partial output is what an
            # operator needs to see why a run timed out, and it belongs in `job.output`.
            exc.agent_output = self._failure_output(stdout, stderr)  # type: ignore[attr-defined]
            raise exc

        return subprocess.CompletedProcess(
            args=cmd, returncode=proc.returncode, stdout=stdout, stderr=stderr,
        )

    def _extract_session_id_from_partial(self, stdout: str, stderr: str) -> str | None:
        """Best-effort session id extraction from partial output after a timeout."""
        try:
            fake_proc = subprocess.CompletedProcess(
                args=[], returncode=1, stdout=stdout, stderr=stderr,
            )
            result = self._parse_output(self._extract_raw(fake_proc))
            return result.session_id
        except Exception:
            return None

    def _execute_and_parse(
        self, cmd: list[str], env: dict, request: RunRequest
    ) -> RunResult:
        """Execute, parse, stamp metadata, report usage."""
        ctx = self.context
        # Metadata only, at every level. The argv is never logged — see _cmd_metadata.
        self._log_cmd = self._cmd_metadata(cmd, request)
        self.logger.info(f"{ctx.harness}-cli exec: {self._log_cmd}")

        # A command is argv plus stdin. Builders that do not use stdin return None
        # (getattr keeps a third-party builder predating this contract working).
        payload = getattr(self.command_builder, "stdin_payload", lambda *_: None)(
            ctx, request
        )
        proc = self._execute_process(cmd, env, payload)
        self.logger.info(
            f"{ctx.harness}-cli rc={proc.returncode} "
            f"stdout={len(proc.stdout)}b stderr={len(proc.stderr)}b"
        )
        # stderr is NOT logged: a harness can echo the prompt (or a credential the CLI
        # printed) there, and DEBUG is not an exemption from "content never enters logs".
        # The content is preserved where content belongs — `job.output` — via the
        # `agent_output` attached to the failure below.

        raw = self._extract_raw(proc)
        try:
            result = self._parse_output(raw)
        except Exception as exc:
            # A classified failure (auth, usage limit, transient) is raised from the parser
            # BEFORE the generic rc!=0 branch below, so it would otherwise carry no output
            # and leave `job.output` empty on the most common failure modes.
            if getattr(exc, "agent_output", None) is None:
                exc.agent_output = raw  # type: ignore[attr-defined]
            raise
        result.harness = str(ctx.harness)
        result.provider = str(ctx.provider)
        result.model = result.model or request.model or ctx.model
        if self.usage_callback:
            try:
                self.usage_callback(result)
            except Exception:
                self.logger.warning("usage_callback failed")

        if proc.returncode != 0:
            # Metadata only in the message: it is persisted verbatim to
            # `job.error_message`, which operators read and log shippers ingest. The agent's
            # actual output rides along on the exception so the consumer can store it in
            # `job.output` — the column already meant for agent output — instead of it being
            # either lost or smuggled through an error string.
            err = RuntimeError(
                f"{ctx.harness} exited with code {proc.returncode} "
                f"(stdout={len(proc.stdout)}b stderr={len(proc.stderr)}b; "
                f"agent output stored in job.output)"
            )
            err.session_id = result.session_id  # type: ignore[attr-defined]
            err.agent_output = self._failure_output(proc.stdout, proc.stderr)  # type: ignore[attr-defined]
            raise err
        return result
