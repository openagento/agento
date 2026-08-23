from __future__ import annotations

import re
import subprocess
import time

from agento.framework.harness import (
    McpInitReport,
    McpServerStatus,
    RunResult,
    SubprocessRunner,
)

from .output_parser import classify_error, parse_session_id, parse_stream

# Pi's own warning when a requested model matched nothing at all. Anchored to the full
# phrase, not a substring. This is the ONLY decision taken from stderr, and it yields an
# ordinary run error — never a credential verdict.
_MODEL_NOT_FOUND_RE = re.compile(
    r'Model\s+"[^"]*"\s+not\s+found\s+for\s+provider\s+"[^"]*"', re.IGNORECASE
)


class PiSubprocessRunner(SubprocessRunner):
    """Runs the Pi CLI. Commands and stdin come from PiCommandBuilder."""

    def _credential_env(self, credential: object | None) -> dict[str, str]:
        if credential is None:
            return {}
        from .config import PiWorkspaceAdapter

        return PiWorkspaceAdapter().credential_env(credential)

    def _try_parse_session_id(self, line: str) -> str | None:
        return parse_session_id(line)

    def _extract_raw(self, proc: subprocess.CompletedProcess) -> str:
        """stdout ONLY.

        The base implementation falls back to stderr when stdout is empty, which would
        feed Pi's diagnostics into the NDJSON path and expose the classifier to text it
        must never judge. Codex overrides this for the same reason.

        Stderr is kept aside for the single anchored model-not-found check below — a
        diagnostic read, never a credential decision.
        """
        self._stderr = proc.stderr or ""
        return proc.stdout or ""

    def execute(self, request):
        # Start the clock here, but READ it in `_parse_output` — which runs inside
        # `super().execute()`, BEFORE `_record_usage`. Assigning the elapsed time in a
        # `finally` (as an earlier version did) happens after the result is already built
        # and usage already recorded, so `duration_ms` was None on the first run and stale
        # on a reused runner.
        self._started_monotonic = time.monotonic()
        return super().execute(request)

    def _parse_output(self, raw: str) -> RunResult:
        parsed = parse_stream(raw)

        # EVERY captured assistant error is fatal.
        #
        # Pi's `--mode json` does NOT promote an assistant error to a non-zero exit — that
        # check is guarded by `if (mode === "text")` (dist/modes/print-mode.js:110-118). So
        # a run that ended in an assistant error exits 0, and returning a normal RunResult
        # here records the job as SUCCESS with the work undone. Raising only on recognised
        # credential phrases (as an earlier version did) left every other Pi error — "bad
        # request: unsupported option", a tool-schema rejection, a context-length error —
        # silently successful.
        #
        # Credential classification applies ONLY when the provider actually uses a
        # credential. Ollama declares `credential_required: false`, so a connection failure
        # there is an ordinary run failure: there is no credential to throttle or poison,
        # and mapping it to TransientAuthError would take an unrelated credential out of
        # the pool.
        if parsed.error_message:
            exc = (
                classify_error(parsed.error_message)
                if self.context.credential_required
                else None
            )
            raise exc or RuntimeError(f"Pi run failed: {parsed.error_message[:500]}")

        self._assert_requested_model_ran(parsed)

        # The init record is read from the TRANSCRIPT, not from stdout.
        #
        # The bridge appends it during `session_start`, and Pi's print mode calls
        # `session.bindExtensions()` (which emits `session_start` synchronously) BEFORE it
        # attaches the JSON-stream subscriber — `dist/modes/print-mode.js:53` vs `:84`
        # inside the same `rebindSession()`. So the resulting `entry_appended` event is
        # emitted with nothing listening on stdout and can never appear there. It does
        # reach the session file, because the session manager persists it, which is why
        # this reads the transcript.
        #
        # `mcp_init_raw` from the stream is still honoured if a future Pi release emits it
        # after the subscriber attaches, but it is not the primary path.
        raw_init = parsed.mcp_init_raw or self._read_init_from_transcript(parsed.session_id)
        mcp_init = None
        if raw_init is not None:
            status = raw_init.get("status")
            mcp_init = McpInitReport(
                servers=(
                    McpServerStatus(
                        name="toolbox",
                        status=str(status) if status else "connected",
                    ),
                )
            )

        return RunResult(
            raw_output=parsed.text or raw,
            input_tokens=parsed.input_tokens or None,
            output_tokens=parsed.output_tokens or None,
            # cost_usd stays None: Pi prices from its own model catalogue, and a
            # generated models.json (Ollama) carries no rates, so any number here
            # would be fiction. cost_reporting is declared false.
            cost_usd=None,
            num_turns=parsed.num_turns or None,
            duration_ms=self._elapsed_ms(),
            session_id=parsed.session_id,
            model=parsed.model,
            provider=parsed.provider,
            mcp_init=mcp_init,
        )

    def _read_init_from_transcript(self, session_id: str | None) -> dict | None:
        """Best-effort: the bridge's init entry, from the session file.

        Telemetry only — a failure here must never fail the run, so every error is
        swallowed. Returns ``None`` when the transcript is absent (interactive runs, a
        wiped run dir) or unreadable.
        """
        if not session_id:
            return None
        try:
            from .transcript_reader import PiTranscriptReader

            return PiTranscriptReader().read_toolbox_init(session_id)
        except Exception:
            return None

    @staticmethod
    def _same_model(actual: str | None, wanted: str | None) -> bool:
        """Compare model ids, tolerating Pi's ``~`` alias marker.

        Found by spike S2 against the live API: requesting
        ``anthropic/claude-haiku-latest`` makes Pi report
        ``~anthropic/claude-haiku-latest`` — the leading ``~`` marks a catalogue alias
        (a "-latest" style moving target). Strict equality therefore FAILED a completely
        legitimate run, which would have broken every aliased model.

        Only the marker is normalised; anything else remains a mismatch, so a genuine
        silent substitution is still caught.
        """
        if actual is None or wanted is None:
            return False
        return actual.lstrip("~") == wanted.lstrip("~")

    def _elapsed_ms(self) -> int | None:
        started = getattr(self, "_started_monotonic", None)
        if started is None:
            return None
        return int((time.monotonic() - started) * 1000)

    def _assert_requested_model_ran(self, parsed) -> None:
        """Fail the run when Pi silently substituted a different model.

        `tryMatchModel` (dist/core/model-resolver.js:104-127) tries an exact match and
        then falls back to a **substring** match on the model's id OR name, returning the
        highest-sorting alias among the hits — emitting nothing. Pi's own warning fires
        only when there was no match at all, so watching for it catches the loud case and
        misses the dangerous one. The positive check is the authoritative one: compare
        what actually ran against what was asked for.

        A mismatch is an ordinary run failure, never a credential verdict: the credential
        is fine, the configuration is not.
        """
        wanted_model = self.context.model
        wanted_provider = str(self.context.provider) if self.context.provider else None

        # Router/meta models dispatch to a different model BY DESIGN — OpenRouter's
        # `openrouter/free` is documented as "a router that selects free models at random"
        # (architecture.tokenizer == "Router" in its /api/v1/models record). Verified live:
        # requesting `openrouter/free` ran `poolside/laguna-xs-2.1:free`. That is correct
        # behaviour, so the identity check must be switched off explicitly for it — and
        # only explicitly, because guessing from the id would create both false positives
        # and false negatives. The PROVIDER check stays on.
        if self.context.harness_config.get("allow_model_substitution") == "1":
            wanted_model = None

        # EVERY assistant identity is checked, not only the last: a mid-run switch would
        # otherwise be hidden by the final message. An assistant response reporting no
        # identity is ALSO a failure — treating that as success is how a silent
        # substitution slips through.
        if (wanted_model or wanted_provider) and not parsed.identities:
            # A stream with no assistant identity at all cannot prove the right model ran.
            # Treating that as success is exactly how a silent substitution slips through.
            raise RuntimeError(
                "Pi reported no assistant provider/model, so the requested "
                f"{wanted_provider}/{wanted_model} could not be verified."
            )

        for provider, model in parsed.identities:
            if wanted_model and not self._same_model(model, wanted_model):
                raise RuntimeError(
                    f"Pi ran model {model!r} but {wanted_model!r} was requested. Pi "
                    f"resolves an unmatched model by silent substring matching, so "
                    f"agent_view/model must be an exact catalogue id."
                )
            if wanted_provider and provider != wanted_provider:
                raise RuntimeError(
                    f"Pi ran provider {provider!r} but {wanted_provider!r} was requested."
                )

        # The bridge performs the same comparison in-process on every spawn path and
        # records it; that entry survives even when this comparison sees no identity.
        if parsed.model_mismatch:
            raise RuntimeError(
                f"Pi reported a model mismatch: {parsed.model_mismatch}. "
                f"agent_view/model must be an exact catalogue id."
            )

        stderr = getattr(self, "_stderr", "") or ""
        if _MODEL_NOT_FOUND_RE.search(stderr):
            raise RuntimeError(
                "Pi could not resolve the requested model and fell back to a synthesised "
                "one (its context window, token limits and pricing belong to a different "
                "model). Set agent_view/model to an exact catalogue id."
            )
