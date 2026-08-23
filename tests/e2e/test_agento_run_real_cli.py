"""End-to-end happy path for every credential type via ``agento run``.

The three axes are independent (harness / provider / model), so a case is a
``(harness, provider, credential_type)`` triple, not a provider alone. For each
one we drive the REAL pipeline:
- point the agent_view at that harness AND its provider via scoped config
  (``agent_view/harness`` selects the program, ``agent_view/provider`` the model
  vendor — setting only the latter would run the OLD harness against the new
  vendor's model and 404),
- pick a cheap model for the harness (``agent_view/model``),
- force the credential under test to win pool selection by giving it the lowest
  priority (``credential:set-priority``; lower wins),
- run ``agento run <agent_view> "<prompt>"`` which shells into the Docker
  sandbox, materializes the credential, and invokes the real agent CLI,
- assert the run exits 0 (auth + model + execution all worked).

The matrix is DISCOVERED, never hardcoded: harness/provider/credential_scope come
from the ``agent_harnesses`` declarations of enabled modules (same source the
config select fields use) and the credential types from the live pool. A new
harness module is covered by adding it to ``_CHEAP_MODEL_BY_HARNESS`` — no other
edit here.

Marked ``@pytest.mark.e2e`` because they invoke the real Docker stack and the
real provider APIs (real money). They are gated behind a single explicit opt-in
because they also TEMPORARILY MUTATE the deployment DB (one credential's priority
and the agent_view's harness/provider/model), restored in a ``finally`` block:

    AGENTO_E2E=1 bin/test

The agent_view to drive is auto-discovered (the first row of ``agento
workspace:build-status``) — there is no env override. Teardown captures the
agent_view-scoped harness/provider/model overrides BEFORE the run and restores
them afterwards — setting them back if they existed, or removing them only if
they were unset — so pre-existing config for that agent_view is preserved, not
erased.

All DB/run interaction goes through the ``agento`` CLI (which proxies into the
containers); the deployment DB is not reachable directly from the host.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parents[2]
_AGENTO = shutil.which("agento") or str(_PROJECT_ROOT / "bin" / "agento")
_E2E_ENABLED = os.environ.get("AGENTO_E2E") == "1"

# Cheap model per harness — keeps the real run inexpensive. The harness ids
# themselves are discovered; only "which model is cheap" needs a human. A harness
# missing here is skipped with an explicit reason rather than failing the suite.
# `pi` uses a FREE OpenRouter model, so its case costs nothing. Deliberately a
# concrete model and not a router (`openrouter/free`, `openrouter/auto`, …): a router
# dispatches to a different model by design, which Pi reports as the model that ran, and
# the identity guard would correctly flag that as a mismatch. Routers need
# `pi/allow_model_substitution=1`; a concrete id exercises the STRICT path instead.
_CHEAP_MODEL_BY_HARNESS = {
    "claude": "haiku",
    "codex": "gpt-5.4-mini",
    "pi": "nvidia/nemotron-3.5-lightning:free",
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
        _PROJECT_ROOT / "src" / "agento" / "modules",
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

pytestmark = [
    pytest.mark.e2e,
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
def test_agento_run_happy_path_per_credential_type(
    harness: str, provider: str, credential_scope: str, credential_type: str,
):
    model = _CHEAP_MODEL_BY_HARNESS.get(harness)
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
    old_priority = credential["priority"]
    prior_harness = _agent_view_override("agent_view/harness")
    prior_provider = _agent_view_override("agent_view/provider")
    prior_model = _agent_view_override("agent_view/model")

    try:
        # Steer the run: harness FIRST (it narrows which providers are valid),
        # then the provider, then the cheap model — and force this exact
        # credential to win pool selection.
        _agento_ok(["config:set", "agent_view/harness", harness, "--agent-view", _AGENT_VIEW])
        _agento_ok(["config:set", "agent_view/provider", provider, "--agent-view", _AGENT_VIEW])
        _agento_ok(["config:set", "agent_view/model", model, "--agent-view", _AGENT_VIEW])
        _agento_ok(["credential:set-priority", str(credential["id"]), str(_FORCE_PRIORITY)])

        run = _agento(
            ["run", _AGENT_VIEW, "Reply with exactly the word: pong"],
            timeout=300,
        )
        assert run.returncode == 0, (
            f"`agento run` failed for {harness}/{provider}/{credential_type} "
            f"(model={model}): rc={run.returncode}\n"
            f"stdout={run.stdout[-800:]}\nstderr={run.stderr[:800]}"
        )
        assert run.stdout.strip(), "agent produced no output"
    finally:
        # Restore exactly what we changed (best-effort): credential priority and
        # the agent_view-scoped overrides. Harness first again, so restoring the
        # provider is validated against the harness it belongs to.
        _agento(["credential:set-priority", str(credential["id"]), str(old_priority)])
        _restore_override("agent_view/harness", prior_harness)
        _restore_override("agent_view/provider", prior_provider)
        _restore_override("agent_view/model", prior_model)
