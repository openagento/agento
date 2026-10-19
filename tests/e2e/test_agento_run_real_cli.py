"""End-to-end happy path for every credential type, through the consumer.

The three axes are independent (harness / provider / model), so a case is a
``(harness, provider, credential_type)`` triple, not a provider alone. For each
one we drive the REAL pipeline with ``agento e2e``:
- insert one test job for the discovered agent_view that PINS its harness and a
  cheap model (``--harness`` / ``--model``, see docs/cli/job-runtime.md) — the
  agent_view's own config is never changed, so its real cron jobs keep running
  on what it is configured for,
- name the credential under test (``--credential``); ``agento e2e`` disables the
  other credentials of that scope for the run and re-enables them after,
- claim exactly that job and run it in the sandbox: credential materialization,
  the real agent CLI, the toolbox, finalize,
- assert the command passed its checks (SUCCESS, model, tokens, output, toolbox
  connected).

The provider is never pinned: it follows the pinned harness (its
``default_provider``), so only default-provider cases are discovered.

The matrix is DISCOVERED, never hardcoded: harness/provider/credential_scope come
from the ``agent_harnesses`` declarations of enabled modules (same source the
config select fields use) and the credential types from the live pool. A new
harness module is covered by adding it to ``_CHEAP_MODEL_BY_HARNESS`` — no other
edit here.

Marked ``@pytest.mark.e2e`` because they invoke the real Docker stack and the
real provider APIs (real money):

    AGENTO_E2E=1 bin/test

The agent_view to drive is auto-discovered (the first row of ``agento
workspace:build-status``) — there is no env override. The steering helpers below
(``_agent_view_override`` / ``_restore_override`` / ``_FORCE_PRIORITY``) serve the
per-harness contract suites, which still run ``agento run`` against view config.

All DB/run interaction goes through the ``agento`` CLI (which proxies into the
containers); the deployment DB is not reachable directly from the host.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess

import pytest

from tests.e2e._stack import AGENTO as _AGENTO
from tests.e2e._stack import MODULES_DIR as _MODULES_DIR
from tests.e2e._stack import PROJECT_ROOT as _PROJECT_ROOT

_E2E_ENABLED = os.environ.get("AGENTO_E2E") == "1"

# Cheap model per harness — keeps the real run inexpensive. The harness ids
# themselves are discovered; only "which model is cheap" needs a human. A harness
# missing here is skipped with an explicit reason rather than failing the suite.
# `pi` uses a paid OpenRouter model at a fraction of a cent per run: the `:free` ids
# answer 429 from a shared upstream pool, and the id must be in Pi's catalogue or its
# guard fails the run. Deliberately a concrete model and not a router (`openrouter/free`,
# `openrouter/auto`, …): a router dispatches to a different model by design, which Pi
# reports as the model that ran, and the identity guard would correctly flag that as a
# mismatch. Routers need
# `pi/allow_model_substitution=1`; a concrete id exercises the STRICT path instead.
_CHEAP_MODEL_BY_HARNESS = {
    "claude": "haiku",
    "codex": "gpt-5.4-mini",
    "pi": "openai/gpt-oss-20b",
}

# A ChatGPT-account login (Codex `oauth`) refuses the API-only mini models
# ("not supported when using Codex with a ChatGPT account"), so it needs its own model.
_MODEL_BY_CREDENTIAL_TYPE = {
    ("codex", "oauth"): "gpt-5.6-luna",
}

# Credential types not run here. A codex_access_token gets 403 from the ChatGPT
# backend (owner decision 2026-10-15: do not cover it in the happy path).
_SKIPPED_TYPES = {
    ("codex", "codex_access_token"): "codex access-token happy path is not covered",
}

# Far below any real priority so the chosen credential deterministically wins
# selection (ORDER BY priority ASC) regardless of the others.
_FORCE_PRIORITY = -1_000_000


def _agento(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_AGENTO, *args],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _agento_ok(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    res = _agento(args, timeout=timeout)
    assert res.returncode == 0, (
        f"`agento {' '.join(args)}` failed: rc={res.returncode}\n"
        f"{res.stdout[-600:]}{res.stderr[-600:]}"
    )
    return res


def _enabled_modules() -> dict[str, bool]:
    """``app/etc/modules.json`` — a module absent from the file is enabled."""
    path = _PROJECT_ROOT / "app" / "etc" / "modules.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _harness_declarations() -> list[dict]:
    """``agent_harnesses`` entries from every ENABLED module's di.json.

    Same on-disk source the ``harness`` / ``provider`` select fields resolve their
    options from, so the matrix cannot drift from what config:set will accept.
    """
    disabled = {name for name, on in _enabled_modules().items() if on is False}
    roots = [
        _MODULES_DIR,
        _PROJECT_ROOT / "app" / "code",
    ]
    harnesses: list[dict] = []
    for root in roots:
        if not root.is_dir():
            continue
        # core modules live at <root>/<name>/di.json, user modules may nest one
        # level deeper (<root>/<vendor>/<name>/di.json).
        for di_path in sorted(list(root.glob("*/di.json")) + list(root.glob("*/*/di.json"))):
            module_dir = di_path.parent
            module_json = module_dir / "module.json"
            name = module_dir.name
            with contextlib.suppress(OSError, ValueError):
                name = json.loads(module_json.read_text()).get("name", name)
            if name in disabled:
                continue
            try:
                declared = json.loads(di_path.read_text()).get("agent_harnesses") or []
            except (OSError, ValueError):
                continue
            harnesses.extend(h for h in declared if isinstance(h, dict) and h.get("id"))
    return harnesses


def _healthy_credentials() -> list[dict]:
    """Healthy pool rows, or [] when the stack is down — an empty matrix then
    skips the module with a reason instead of erroring during collection."""
    res = _agento(["credential:list", "--json"])
    if res.returncode != 0:
        return []
    try:
        rows = json.loads(res.stdout)
    except ValueError:
        return []
    return [c for c in rows if c.get("status") == "ok" and c.get("enabled", True)]


def _discover_cases() -> list[tuple[str, str, str, str]]:
    """(harness, provider, credential_scope, credential_type) — one case per
    credential type the pool holds for a harness/provider's credential scope.

    The scope travels WITH the case: it is the provider's declared
    ``credential_scope``, which is not required to equal the harness id (it
    happens to for claude/codex). Re-deriving it from the harness id in the test
    body would silently skip a harness that names its pool differently."""
    if not _E2E_ENABLED:
        return []
    types_by_scope: dict[str, set[str]] = {}
    for cred in _healthy_credentials():
        scope = cred.get("scope") or cred.get("agent_type")
        if scope and cred.get("type"):
            types_by_scope.setdefault(scope, set()).add(cred["type"])

    cases: list[tuple[str, str, str, str]] = []
    for harness in _harness_declarations():
        for provider in harness.get("providers") or []:
            # A pinned job's provider is the harness default; another one cannot be pinned.
            if provider.get("id") != harness.get("default_provider"):
                continue
            if not provider.get("credential_required", True):
                continue
            scope = provider.get("credential_scope")
            for cred_type in sorted(types_by_scope.get(scope, ())):
                case = (harness["id"], provider["id"], scope, cred_type)
                if case not in cases:
                    cases.append(case)
    return cases


def _discover_agent_view() -> str | None:
    """The agent_view to drive: the first row of ``agento workspace:build-status``
    (the most-recently-built agent_view). Returns None when the stack is down or
    nothing has been built yet — the suite then skips. Rows look like
    ``<id> <code> <checksum> <status> ...``; the header and the ``---`` separator
    have a non-numeric first column, so we take the first numeric-id row."""
    res = _agento(["workspace:build-status"])
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            return parts[1]
    return None


# Always auto-discovered — no env override. Resolved only when enabled so a
# normal / ``--fast`` collection never shells out to the CLI.
_AGENT_VIEW = _discover_agent_view() if _E2E_ENABLED else None
_CASES = _discover_cases()

# The stack e2e tests (this file, test_claude_real_cli, test_refresh_real_cli) steer the SAME
# agent_view config and credential rows, so under pytest-xdist they share one group and run
# one after another on a single worker (`--dist loadgroup`).
E2E_STACK = pytest.mark.xdist_group("e2e_stack")

pytestmark = [
    pytest.mark.e2e,
    E2E_STACK,
    pytest.mark.skipif(
        not _E2E_ENABLED,
        reason="set AGENTO_E2E=1 to run (spends real provider credentials, mutates the pool)",
    ),
    pytest.mark.skipif(
        _E2E_ENABLED and not _AGENT_VIEW,
        reason="no agent_view found — start the stack and build one (agento workspace:build)",
    ),
    pytest.mark.skipif(
        _E2E_ENABLED and bool(_AGENT_VIEW) and not _CASES,
        reason="no healthy credential matches any enabled harness's credential scope",
    ),
]


def _agent_view_override(path: str) -> str | None:
    """Return the value of ``path`` set at THIS agent_view's scope, or None when
    no agent_view-scoped override exists (unset, or the value is shared with
    another scope — in which case removing it leaves the effective value
    unchanged). ``config:get`` prints per-scope lines tagged like
    ``[agent_view: <code>]``; a deduplicated single line (all scopes equal)
    carries no tag and safely maps to None."""
    res = _agento(["config:get", path])
    tag = f"[agent_view: {_AGENT_VIEW}]"
    for line in res.stdout.splitlines():
        if tag in line and " = " in line:
            return line.split(" = ", 1)[1].rsplit("  [", 1)[0].strip()
    return None


def _restore_override(path: str, prior: str | None) -> None:
    if prior is not None:
        _agento(["config:set", path, prior, "--agent-view", _AGENT_VIEW])
    else:
        _agento(["config:remove", path, "--agent-view", _AGENT_VIEW])


@pytest.mark.parametrize(
    ("harness", "provider", "credential_scope", "credential_type"),
    _CASES,
    ids=[f"{h}-{p}-{t}" for h, p, _s, t in _CASES],
)
def test_pinned_job_happy_path_per_credential_type(
    harness: str, provider: str, credential_scope: str, credential_type: str,
):
    if (harness, credential_type) in _SKIPPED_TYPES:
        pytest.skip(_SKIPPED_TYPES[(harness, credential_type)])
    model = _MODEL_BY_CREDENTIAL_TYPE.get(
        (harness, credential_type), _CHEAP_MODEL_BY_HARNESS.get(harness)
    )
    if model is None:
        pytest.skip(f"no cheap model registered for harness {harness!r} — add one to run this")
    candidates = [
        c for c in _healthy_credentials()
        if (c.get("scope") or c.get("agent_type")) == credential_scope
        and c["type"] == credential_type
    ]
    if not candidates:
        pytest.skip(f"no healthy {credential_scope}/{credential_type} credential registered")
    credential = candidates[0]

    res = _agento(
        ["e2e", "--agent-view", _AGENT_VIEW, "--harness", harness, "--model", model,
         "--credential", str(credential["id"])],
        timeout=600,
    )
    if res.returncode != 0 and "reached your specified API usage limits" in res.stdout:
        # The account's spend cap, not a defect: nothing can be checked until it resets.
        pytest.skip(f"{harness}/{credential_type}: provider usage limit reached")
    assert res.returncode == 0, (
        f"`agento e2e` failed for {harness}/{provider}/{credential_type} (model={model}):\n"
        + "\n".join(line for line in res.stdout.splitlines()
                    if "[FAIL]" in line or "ERROR" in line)
        + f"\nstderr={res.stderr[-800:]}"
    )
