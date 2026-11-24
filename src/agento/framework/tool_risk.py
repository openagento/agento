"""The lethal-trifecta assessment of one run, frozen at its claim (PRD tool labels §5).

A run is at risk when the tools it may really call carry, between them, all three risk
labels: ``private-data``, ``untrusted-content`` and ``external-communication``. The input is
the toolbox gate for the run's view (``is_enabled`` + ``requires``) and the dispatcher's
stub decision for the run's test policy: a stubbed or refused tool supplies nothing.

Written twice: at the execution mint (``direct: "pending"``) and once after the workspace,
when the harness has reported the MCP servers that bypass the toolbox (``with_direct``).
An error never blocks a run: it is recorded as the error object.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .module_loader import ModuleManifest
from .test_run import (
    LabelSnapshot,
    SnapshotTooLarge,
    default_labels_by_tool,
    effective_labels,
    is_stubbed,
)
from .tool_catalog import RISK_KEYS

logger = logging.getLogger(__name__)

VERSION = 1
MAX_CATEGORY_NAMES = 50
MAX_UNCOVERED = 20
CONDITIONAL = "conditional"
NOTE_MODULES_CHANGED = "modules_changed"
NOTE_CONDITIONAL = "conditional_tools"
NOTE_SNAPSHOT_TOO_LARGE = "snapshot_too_large"


@dataclass(frozen=True)
class AssessTool:
    id: str
    name: str
    default_labels: frozenset[str]
    labels: frozenset[str]
    conditional: bool


def bounded(names: Iterable[str], limit: int) -> list[str]:
    """Sorted, unique, at most ``limit`` names, then ``"…+N"`` for the rest (CODE-8)."""
    names = sorted(set(names))
    return names if len(names) <= limit else [*names[:limit], f"…+{len(names) - limit}"]


def error_assessment(exc: BaseException | None = None) -> dict:
    """The assessment of a run whose assessment failed. Logs the exception class only (SEC-6)."""
    if exc is not None:
        logger.warning("Tool risk assessment failed: %s", type(exc).__name__)
    return {"v": VERSION, "trifecta": False, "tool_coverage": "error", "coverage": "error",
            "direct": "pending", "categories": {}, "uncovered": [],
            "notes": [NOTE_SNAPSHOT_TOO_LARGE] if isinstance(exc, SnapshotTooLarge) else []}


def _covers(tools: Iterable[AssessTool]) -> bool:
    return set(RISK_KEYS) <= {label for t in tools for label in t.labels}


def assess(tools: Sequence[AssessTool], available: set[str], policy: dict | None, *,
           modules_changed: bool) -> dict:
    """The mint-time assessment. Pure."""
    counted = [t for t in tools
               if t.name in available and is_stubbed(policy, t.id, t.default_labels) is False]
    notes = []
    tool_coverage = "complete"
    if modules_changed:
        tool_coverage = "incomplete"
        notes.append(NOTE_MODULES_CHANGED)
    if _covers(t for t in counted if not t.conditional):
        trifecta: bool | str = True
    elif _covers(counted):
        trifecta = "possible"
        tool_coverage = "incomplete"
        notes.append(NOTE_CONDITIONAL)
    else:
        trifecta = False
    return {
        "v": VERSION, "trifecta": trifecta, "tool_coverage": tool_coverage,
        "coverage": _coverage(tool_coverage, None, pending=True), "direct": "pending",
        "categories": {key: bounded((t.name for t in counted if key in t.labels), MAX_CATEGORY_NAMES)
                       for key in RISK_KEYS},
        "uncovered": [], "notes": notes,
    }


def _coverage(tool_coverage: str, direct: list[str] | None, *, pending: bool = False) -> str:
    if tool_coverage == "error":
        return "error"
    if pending or tool_coverage == "incomplete" or direct is None or direct:
        return "incomplete"
    return "complete"


def with_direct(tool_risk: dict, direct: list[str] | None) -> dict:
    """The assessment after the workspace: ``direct`` is the harness's report of the MCP
    servers that bypass the toolbox, ``None`` when the harness cannot tell. Pure."""
    return {**tool_risk, "direct": "recorded", "uncovered": bounded(direct or (), MAX_UNCOVERED),
            "coverage": _coverage(tool_risk["tool_coverage"], direct)}


def assess_tools(manifests: Iterable[ModuleManifest], snapshot: LabelSnapshot) -> list[AssessTool]:
    """Every bootstrapped tool with its manifest defaults and its effective labels."""
    manifests = list(manifests)
    defaults = default_labels_by_tool(manifests)
    labels = effective_labels(manifests, snapshot, defaults=defaults)
    conditional = {
        f"{m.name}/{t['name']}" for m in manifests for t in m.tools
        if isinstance(t, dict) and t.get("availability") == CONDITIONAL
    }
    return [AssessTool(id=tid, name=tid.partition("/")[2], default_labels=defaults[tid],
                       labels=labels[tid], conditional=tid in conditional)
            for tid in defaults]


def assess_run(conn, agent_view_id: int | None, policy: dict | None, snapshot: LabelSnapshot | None,
               manifests: Iterable[ModuleManifest], *, error: BaseException | None = None) -> dict:
    """The mint-time assessment of one claim; never raises.

    ``snapshot`` is the claim's one label snapshot; ``None`` with ``error`` when reading it
    failed. A run with no agent_view gets no toolbox capability, so it has no tool.
    """
    from .tool_enablement import effective_tools_for_view

    if snapshot is None:
        return error_assessment(error)
    try:
        tools = assess_tools(manifests, snapshot)
        available = (set() if agent_view_id is None
                     else effective_tools_for_view(conn, agent_view_id, {t.name for t in tools}))
        return assess(tools, available, policy, modules_changed=modules_changed_since_bootstrap())
    except Exception as exc:
        return error_assessment(exc)


_CHANGED_AT: list[float] = []


def modules_changed_since_bootstrap() -> bool:
    """Whether a module file changed after the last ``bootstrap()``: a busy consumer does
    not reload, while the toolbox reads module files per session (F30).

    Reads ``app/etc/modules.json`` and each enabled module's ``module.json`` and
    ``config.json``. A positive answer is kept until the next bootstrap.
    """
    from .bootstrap import bootstrapped_at, get_manifests
    from .module_status import _resolve_path

    started = bootstrapped_at()
    if started is None:
        return False
    if _CHANGED_AT and _CHANGED_AT[0] == started:
        return True
    paths = [_resolve_path()]
    for m in get_manifests():
        paths += [m.path / "module.json", m.path / "config.json"]
    changed = any(_mtime_ns(p) > started for p in paths)
    if changed:
        _CHANGED_AT[:] = [started]
    return changed


def _mtime_ns(path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0
