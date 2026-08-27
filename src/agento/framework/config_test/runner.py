"""Dispatch one config test — and nothing else.

Two arms, chosen by the declaration's kind:

* **toolbox** (everything except ``local``) — hand the field path to the
  toolbox, which resolves and probes where the secret already is. This side
  resolves nothing.
* **local** — import the class the declaring module named, from that module's own
  directory, and let it resolve whatever config it already reads. This side hands
  it nothing but the connection and the scope.

So there is no "may this tester decrypt?" question to answer here: the framework
never decrypts on a tester's behalf. What remains is the boring part — a
declaration that does not resolve, a class that does not import, a tester that
raises or returns nonsense — and all of it comes back as ``ERROR`` with a message
safe to print in a TUI toast. ``run_config_test`` never raises.
"""
from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from pathlib import Path

from ..module_loader import import_class
from ..scoped_config import Scope
from .manifest import KIND_LOCAL, tester_for_field
from .protocols import CODE_RE, ERROR, STATUSES, TestResult
from .toolbox import run_toolbox_test

logger = logging.getLogger(__name__)

# Below this length a NON-SECRET value is not worth masking, and masking it
# would corrupt legitimate text (a port "587", a boolean "true"). A value from
# an `obscure` field is masked at any length — see sanitize().
_MIN_MASK_LEN = 4


def sanitize(
    message: str,
    values: Mapping[str, str | None],
    obscure_paths: Collection[str] = (),
) -> str:
    """Mask any value that appears verbatim in ``message``, and flatten it.

    A value resolved from an ``obscure`` field is masked whatever its length: the
    length floor exists to keep ports and booleans readable, and must never be
    the reason a short password survives. Longest-first, so a value that
    contains another is masked as a whole.

    A **local** tester is the caller — it resolved its own values, so it is the
    only side that can mask them. The toolbox arm redacts at its source instead
    (Task 1, Step 8), which also covers probes no contract here can bind.
    """
    out = str(message).replace("\n", " ").strip()
    out = " ".join(out.split())
    obscure = set(obscure_paths)
    secrets = sorted(
        (
            v for path, v in values.items()
            if v and (path in obscure or len(v) >= _MIN_MASK_LEN)
        ),
        key=len,
        reverse=True,
    )
    for secret in secrets:
        out = out.replace(secret, "***")
    return out


def _scope_error(scope: str, scope_id: int) -> TestResult | None:
    """``None`` when ``(scope, scope_id)`` names a real scope, else the refusal.

    ``default`` is the only scope with no id; ``workspace`` and ``agent_view``
    are keyed by an auto_increment column, so 0 and negatives name nothing.
    """
    if scope == Scope.DEFAULT:
        if scope_id:
            return TestResult(
                ERROR, f"the default scope takes no scope id, got {scope_id!r}",
                code="SCOPE_ID_INVALID",
            )
        return None
    if scope in (Scope.WORKSPACE, Scope.AGENT_VIEW):
        if not (isinstance(scope_id, int) and not isinstance(scope_id, bool) and scope_id > 0):
            return TestResult(
                ERROR, f"the {scope} scope needs a positive id, got {scope_id!r}",
                code="SCOPE_ID_INVALID",
            )
        return None
    return TestResult(
        ERROR,
        f"unknown scope {scope!r} — one of "
        f"{Scope.DEFAULT}, {Scope.WORKSPACE}, {Scope.AGENT_VIEW}",
        code="SCOPE_UNSUPPORTED",
    )


def _clean(result: TestResult) -> TestResult:
    """Enforce the wire contract on a local tester's result."""
    if not isinstance(result, TestResult) or result.status not in STATUSES:
        return TestResult(
            ERROR, "the tester returned something that is not a TestResult",
            code="BAD_RESULT",
        )
    # `isinstance` before the regex: a tester is module code, so `code` is input.
    # `CODE_RE.match(7)` raises TypeError, out through a function documented never
    # to raise and into a TUI worker thread.
    code = result.code if isinstance(result.code, str) and CODE_RE.match(result.code) else ""
    message = " ".join(str(result.message).split())
    return TestResult(result.status, message, code=code)


def run_config_test(
    conn,
    config_path: str,
    *,
    scope: str,
    scope_id: int = 0,
    project_root: Path | None = None,
) -> TestResult:
    """Run the test declared on ``config_path`` at ``(scope, scope_id)``."""
    bad_scope = _scope_error(scope, scope_id)
    if bad_scope is not None:
        # Checked for BOTH arms and before the declaration is read: a local
        # tester builds a `ScopedConfigService`, which treats an unknown scope
        # and a zero id alike as a default-scope read. The verdict would then be
        # about the default credential while the caller named another scope —
        # the misdiagnosis class this whole feature exists to end.
        return bad_scope
    try:
        ref = tester_for_field(config_path, project_root)
    except Exception:
        # `tester_for_field` is documented never to raise; if a future change
        # breaks that, this must still not surface as a traceback in a TUI.
        logger.exception("Reading the tester declaration for %s failed", config_path)
        ref = None
    if ref is None:
        return TestResult(
            ERROR,
            f"'{config_path}' declares no tester in its module's system.json",
            code="NO_TESTER",
        )

    if ref.kind != KIND_LOCAL:
        return run_toolbox_test(conn, config_path, scope=scope, scope_id=scope_id)

    try:
        cls = import_class(ref.module_dir, ref.class_path)
    except Exception as e:
        logger.warning(
            "Config tester %s for %s could not be imported: %s",
            ref.class_path, config_path, e,
        )
        return TestResult(
            ERROR,
            f"the tester declared for '{config_path}' could not be imported "
            f"({type(e).__name__})",
            code="TESTER_UNAVAILABLE",
        )

    try:
        result = cls().run(conn, scope=scope, scope_id=scope_id)
    except Exception as e:
        # The exception TEXT is deliberately dropped — from the RESULT and from
        # the log alike. A tester resolves secrets itself, so its message is the
        # one string here nobody has scrubbed, and the formatter these handlers
        # carry (`log._Formatter`) writes the whole traceback verbatim.
        # `logger.exception` here would put the unscrubbed text in the persistent
        # log, which is a worse place for it than a terminal.
        logger.error(
            "Config tester for %s raised %s", config_path, type(e).__name__,
        )
        return TestResult(
            ERROR, f"the tester raised {type(e).__name__} (see the log)",
            code="TESTER_RAISED",
        )
    return _clean(result)


__all__ = ["run_config_test", "sanitize"]
