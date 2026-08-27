"""The toolbox arm — ask the toolbox to test its own credential.

The toolbox is the only container holding the SMTP password, the Jira token and
the Graph credentials (``CLAUDE.md`` → *Security*), so it runs the probe and this
side only renders the answer:

    POST {toolbox}/config-test?path=<module/field>[&agent_view_id=N]
    -> {"status": "ok|fail|not_configured|error", "code": "...",
        "detail": "...", "ms": 12}

The response is always HTTP 200 with a four-state body: "I could not check" and
"the credential is wrong" are different answers and must not be collapsed into
the transport layer. Calling the toolbox from a CLI command is an established
pattern here — ``modules/bitbucket/src/onboarding.py:169-226`` does the same to
verify a Bitbucket credential.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from ..config_resolver import ScopedConfigService
from .protocols import CODE_RE, ERROR, FAIL, NOT_CONFIGURED, OK, TestResult

DEFAULT_TOOLBOX_URL = "http://toolbox:3001"
# One probe per request, capped at 15 s inside the toolbox (Task 1), so this is
# that plus room for the request itself — not the old 45 s, which existed only
# because /health?test=true fans out to every registered healthcheck.
TIMEOUT_S = 25.0

_STATUS_MAP = {"ok": OK, "fail": FAIL, "not_configured": NOT_CONFIGURED, "error": ERROR}


class ToolboxUrlError(ValueError):
    """``core/toolbox/url`` is not a usable internal endpoint."""


def _resolve_toolbox_url(conn) -> str:
    """``core/toolbox/url``, validated, or the compose default.

    A single per-path read — never ``resolve_all()`` and never ``get_module()``,
    which resolves *every* field of the module (``core`` owns ``smtp_pass``) and
    additionally needs a booted app for its manifest lookup. The URL is not a
    secret; this is the only value the framework resolves in this whole feature.

    It IS a Python request whose destination comes from config. It is the same
    value ``consumer.py:548`` already dials, and only an operator with DB or ENV
    access can set it, so the threat is not an untrusted URL; it is a *malformed*
    one. Three properties are therefore checked rather than trusted, and each has
    bitten an operator somewhere: an unsupported scheme (``file:``, ``gopher:``)
    turning a diagnostic into a local read, user-info (``http://u:p@host``)
    putting a credential into every error message this module prints, and a query
    or fragment silently overriding the ``path`` parameter that says what to test.
    Destination allow-listing is deliberately NOT done here: the endpoint is a
    compose service the operator chooses, and a second, stricter policy than the
    consumer's on the same value would only make the two disagree.
    """
    try:
        raw = ScopedConfigService(conn).get("core/toolbox/url")
    except Exception:
        raw = None
    return _validated_toolbox_url(raw or DEFAULT_TOOLBOX_URL)


def _validated_toolbox_url(raw: str) -> str:
    """Return ``scheme://netloc`` for a usable internal URL, else raise.

    Path, query and fragment are discarded, not rejected: the caller appends
    ``/config-test`` and its own params, so anything after the authority is noise
    at best and an override at worst.

    Every exit is a ``ToolboxUrlError``, including the one from ``urlsplit``
    itself: it raises a bare ``ValueError`` ("Invalid IPv6 URL") on an
    unterminated bracket such as ``http://[::1`` — verified — and
    ``run_toolbox_test`` catches only ``ToolboxUrlError``, so an unwrapped one
    would escape as an uncaught exception and break this module's "never raises"
    contract. The lazy ``.port`` access below is a SECOND, different failure of
    the same parse; both are needed.
    """
    try:
        parsed = urlsplit(raw.strip())
    except ValueError as e:
        raise ToolboxUrlError(f"core/toolbox/url is not a parsable URL ({e})") from None
    if parsed.scheme not in ("http", "https"):
        raise ToolboxUrlError(
            f"core/toolbox/url must be http:// or https://, got {parsed.scheme!r}"
        )
    if parsed.username or parsed.password:
        raise ToolboxUrlError(
            "core/toolbox/url must not embed credentials (user:pass@host) — the "
            "toolbox is reached over the internal network and the URL is printed "
            "in diagnostics"
        )
    if not parsed.hostname:
        raise ToolboxUrlError("core/toolbox/url has no host")
    try:
        _ = parsed.port      # the access itself is the check — see below
    except ValueError:
        # `urlsplit` parses the authority lazily: "toolbox:abc" yields a
        # hostname and only raises when `.port` is touched. httpx then raises
        # InvalidURL — which is NOT an httpx.HTTPError — so an unvalidated port
        # would escape run_toolbox_test's handler as an uncaught exception.
        raise ToolboxUrlError("core/toolbox/url has a non-numeric port") from None
    return f"{parsed.scheme}://{parsed.netloc}"


def _origin(url: str) -> str:
    """A URL safe to print: scheme, host and port, nothing else.

    ``_validated_toolbox_url`` already strips user-info, but every message goes
    through this so that a future caller passing an unvalidated URL cannot leak
    one either. For the same reason the parse itself is guarded: ``urlsplit``
    raises on a malformed authority, and a printing helper that raises turns a
    diagnostic into a traceback.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<unprintable url>"
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        port = ""  # printing must never raise, whatever it was handed
    return f"{parsed.scheme}://{parsed.hostname or '?'}{port}"


SUPPORTED_SCOPES = ("default", "agent_view")


def run_toolbox_test(conn, config_path: str, *, scope: str, scope_id: int = 0) -> TestResult:
    """Ask the toolbox to test ``config_path`` and map its answer.

    Five outcomes are deliberately ``ERROR`` rather than ``FAIL``, because in
    each of them the credential was never actually probed: the requested scope is
    one the toolbox cannot resolve, the configured URL is unusable, the toolbox is
    unreachable (the normal CLI path proxies into the cron container, where the
    compose service name resolves; ``--local`` runs on the host, where it does
    not), it answered with a non-200, or it answered with something this side
    cannot read.
    """
    if scope not in SUPPORTED_SCOPES:
        # `loadScopedDbOverrides(agentViewId)` is the toolbox's only scoped
        # reader, so `workspace` has no chain there. Sending it anyway would
        # silently probe the DEFAULT values and report a verdict about a
        # credential nobody asked about — the exact misdiagnosis class this
        # feature exists to end. The local arm honours every scope; only the
        # toolbox arm is bounded, and it says so.
        return TestResult(
            ERROR,
            f"a toolbox test cannot run at {scope!r} scope — the toolbox resolves "
            f"default and agent_view only",
            code="SCOPE_UNSUPPORTED",
        )
    if scope == "agent_view" and not (isinstance(scope_id, int) and scope_id > 0):
        # Same class as the unsupported scope above, one level finer: dropping a
        # falsy id and sending no `agent_view_id` probed the DEFAULT credential
        # and reported `ok` for a scope the caller named. `agent_view.id` is
        # auto_increment, so 0 and negatives identify nothing.
        return TestResult(
            ERROR,
            f"an agent_view test needs a positive agent_view id, got {scope_id!r}",
            code="SCOPE_ID_INVALID",
        )
    try:
        url = _resolve_toolbox_url(conn)
    except ToolboxUrlError as e:
        return TestResult(ERROR, str(e), code="TOOLBOX_URL_INVALID")

    params: dict[str, str] = {"path": config_path}
    if scope == "agent_view":
        params["agent_view_id"] = str(scope_id)

    try:
        # POST, not GET: this triggers a live authentication attempt against a
        # third party. A side-effecting GET lands in proxy logs and browser
        # history and is replayable from anywhere that can emit a link.
        resp = httpx.post(f"{url}/config-test", params=params, timeout=TIMEOUT_S)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        # InvalidURL is a subclass of Exception, NOT of HTTPError — verified:
        # `issubclass(httpx.InvalidURL, httpx.HTTPError)` is False. Listing it
        # keeps the "never raises" contract even if a future URL shape slips
        # past _validated_toolbox_url.
        return TestResult(
            ERROR,
            f"Toolbox unreachable at {_origin(url)} ({type(e).__name__}) — the "
            f"probe runs inside the toolbox container; run this without --local "
            f"so the CLI proxies into it",
            code="TOOLBOX_UNREACHABLE",
        )
    if resp.status_code != 200:
        return TestResult(
            ERROR,
            f"Toolbox at {_origin(url)} answered HTTP {resp.status_code}",
            code="TOOLBOX_HTTP_ERROR",
        )
    try:
        body = resp.json()
    except ValueError:
        return TestResult(
            ERROR, f"Toolbox at {_origin(url)} returned a non-JSON body",
            code="TOOLBOX_BAD_BODY",
        )
    if not isinstance(body, dict):
        # A JSON array, scalar or null is valid JSON. `.get` on it raises
        # AttributeError, which no handler here catches.
        return TestResult(
            ERROR,
            f"Toolbox at {_origin(url)} returned a {type(body).__name__}, not a JSON object",
            code="TOOLBOX_BAD_BODY",
        )

    # `isinstance` before the lookup: the body is wire data, and
    # `_STATUS_MAP.get([])` raises TypeError (unhashable) out of a function
    # documented never to raise.
    raw_status = body.get("status")
    status = _STATUS_MAP.get(raw_status) if isinstance(raw_status, str) else None
    if status is None:
        return TestResult(
            ERROR, f"Toolbox at {_origin(url)} returned an unknown status",
            code="TOOLBOX_BAD_BODY",
        )

    # `detail` was authored and redacted where the values live (Task 1, Step 8);
    # this side resolved nothing and has nothing to mask with. `code` is returned
    # as-is, which is exactly why its shape is enforced here.
    raw_code = str(body.get("code") or "")
    code = raw_code if CODE_RE.match(raw_code) else ""
    detail = str(body.get("detail") or "").replace("\n", " ").strip()
    ms = body.get("ms")

    if status == OK:
        return TestResult(OK, "ok" + (f" ({ms} ms)" if isinstance(ms, int) else ""), code=code)
    if status == NOT_CONFIGURED:
        return TestResult(NOT_CONFIGURED, detail or "not configured", code=code)
    if status == FAIL:
        return TestResult(FAIL, detail or "failed", code=code)
    return TestResult(ERROR, detail or "the toolbox could not run this test", code=code)


__all__ = ["DEFAULT_TOOLBOX_URL", "ToolboxUrlError", "run_toolbox_test"]
