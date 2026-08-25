"""Redaction barrier for secrets the framework itself minted.

Exact-string replacement of a value we already hold — never a pattern. A pattern
guesses, and a guess either misses a shape or destroys innocent output.
"""
from __future__ import annotations

import contextlib

REDACTION = "***"


def redact_secret(text: str | None, *secrets: str | None) -> str | None:
    """Replace every occurrence of each secret with ``***``."""
    if not text:
        return text
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTION)
    return text


def redact_exception(exc: BaseException, *secrets: str | None) -> BaseException:
    """Strip the secrets from every attribute of ``exc`` that reaches persistence.

    ``args`` drives ``str(exc)``, which lands in ``job.error_message``;
    ``agent_output`` / ``output`` / ``stderr`` land in ``job.output``.
    """
    if not any(secrets):
        return exc
    exc.args = tuple(
        redact_secret(a, *secrets) if isinstance(a, str) else a for a in exc.args
    )
    for attr in ("agent_output", "output", "stderr"):
        value = getattr(exc, attr, None)
        if isinstance(value, str):
            # A __slots__ or read-only attribute must not turn a failure into a crash.
            with contextlib.suppress(AttributeError):
                setattr(exc, attr, redact_secret(value, *secrets))
    return exc
