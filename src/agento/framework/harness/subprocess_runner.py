"""SubprocessRunner — executes a harness CLI for one run and records usage.

Formerly ``agent_manager.runner.TokenRunner``. Two things changed beyond the move:

- **Command building left.** Flags come from the harness's :class:`CommandBuilder`, so
  headless and interactive can no longer drift apart.
- **Credential selection left.** The caller claims the credential once and puts it on
  the :class:`HarnessRunContext`; this runner only consumes it. With two resolvers, one
  run could build its command from one credential and execute against another.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable

from ..ssh_prelude import wrap_with_ssh_prelude
from .protocols import CommandBuilder
from .runtime import HarnessRunContext, RunRequest, RunResult


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
        env = {**os.environ, **self._credential_env(ctx.credential), **ctx.extra_env}
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
    ) -> None:
        """Register the progress callbacks (``Runner`` protocol)."""
        if on_pid is not None:
            self.pid_callback = on_pid
        if on_session_id is not None:
            self.session_id_callback = on_session_id

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

    def _execute_process(self, cmd: list[str], env: dict) -> subprocess.CompletedProcess:
        """Execute a subprocess with incremental output reading.

        Reads stdout/stderr in threads so that the session id can be reported via
        callback immediately, and partial output survives a timeout.
        """
        if self.context.home_dir is not None:
            env = {**env, "HOME": self.context.home_dir}
            spawn_cmd = wrap_with_ssh_prelude(cmd)
        else:
            spawn_cmd = cmd

        proc = subprocess.Popen(
            spawn_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.context.working_dir,
            env=env,
        )

        if self.pid_callback:
            try:
                self.pid_callback(proc.pid)
            except Exception:
                self.logger.warning(f"PID callback failed for pid={proc.pid}")

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        session_id_found: str | None = None

        def _drain(stream, lines: list[str], parse_session: bool) -> None:
            nonlocal session_id_found
            for line in stream:
                lines.append(line)
                if parse_session and session_id_found is None:
                    sid = self._try_parse_session_id(line)
                    if sid:
                        session_id_found = sid
                        if self.session_id_callback:
                            try:
                                self.session_id_callback(sid)
                            except Exception:
                                self.logger.warning(f"session_id_callback failed for sid={sid}")

        stdout_thread = threading.Thread(
            target=_drain, args=(proc.stdout, stdout_lines, True), daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain, args=(proc.stderr, stderr_lines, True), daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        try:
            proc.wait(timeout=self.context.timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            timed_out = True

        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)

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
        """Execute, parse, stamp metadata, record usage."""
        ctx = self.context
        # Metadata only, at every level. The argv is never logged — see _cmd_metadata.
        self._log_cmd = self._cmd_metadata(cmd, request)
        self.logger.info(f"{ctx.harness}-cli exec: {self._log_cmd}")

        proc = self._execute_process(cmd, env)
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
        self._record_usage(result)

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

    # -- usage ----------------------------------------------------------------

    def _get_db_connection(self):
        """Get a DB connection using DatabaseConfig. Best-effort, may raise."""
        from ..database_config import DatabaseConfig
        from ..db import get_connection

        return get_connection(DatabaseConfig.from_env())

    def _record_usage(self, result: RunResult) -> None:
        """Best-effort usage recording — never raises.

        A run without a credential (a provider that needs none) is still recorded,
        attributed by ``(harness, provider)`` with ``credential_id = NULL``.
        """
        from ..agent_manager.usage_store import record_usage

        credential = self.context.credential
        credential_id = getattr(credential, "id", None) if credential is not None else None
        try:
            conn = self._get_db_connection()
            try:
                tokens_used = (result.input_tokens or 0) + (result.output_tokens or 0)
                record_usage(
                    conn,
                    credential_id=credential_id,
                    tokens_used=tokens_used,
                    input_tokens=result.input_tokens or 0,
                    output_tokens=result.output_tokens or 0,
                    duration_ms=result.duration_ms or 0,
                    model=result.model,
                    harness=str(self.context.harness),
                    provider=str(self.context.provider),
                    logger=self.logger,
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            self.logger.exception("Failed to record usage (best-effort, continuing)")
