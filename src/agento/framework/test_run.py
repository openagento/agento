"""Test runs: which tools one run stubs (DECISIONS.md D-TEST-RUN-1).

The public input is the ``--test`` JSON plus any ``--stub-tools <label>`` flags:

    {"stub": ["jira_search"], "real": ["jira_add_comment"]}

Before the run's capability is minted, the input is resolved once into a v2 policy:

    {"v": 2, "stub": [<names>], "real": [<names>], "stub_labels": [<label keys>],
     "unstub": [<module>/<tool>], "known": [<module>/<tool>]}

``stub`` = explicit ``stub`` plus the tools whose effective labels hold a ``--stub-tools`` label or
``stub-in-test``, minus ``real``. ``known`` is every tool the resolution considered; the
toolbox refuses a call to a tool outside it. ``unstub`` holds the tools whose
``stub-in-test`` default an admin removed. The same object is stored on the job (children
and retries reuse it, never re-resolve) and, wrapped in an envelope, on the capability.

A test run never gets the SSH private key. Test mode exists only where
``AGENTO_TEST_MODE_ENABLED=1``; it is off by default, so a production deployment refuses it.
An unknown key is an error, so a newer option never silently does nothing. The JSON travels
in argv: it must never carry a secret.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .config_resolver import read_config_defaults
from .module_loader import ModuleManifest
from .tool_catalog import MAX_LABELS, SOURCE_PREFIX, STUB_IN_TEST, default_labels, is_in_tree

TEST_MODE_ENV = "AGENTO_TEST_MODE_ENABLED"
MAX_TEST_CONFIG_BYTES = 16384
MAX_STUBBED = 1000
MAX_KNOWN = 2000
MAX_SNAPSHOT_ROWS = 20000
POLICY_VERSION = 2
CAPABILITY_ENVELOPE_TAG = "agento-test-policy"
_KEYS = ("stub", "real")
_V2_BOUNDS = {"stub": MAX_STUBBED, "real": MAX_KNOWN, "stub_labels": MAX_LABELS,
              "unstub": MAX_KNOWN, "known": MAX_KNOWN}

ToolKey = tuple[str, str]


class InvalidTestConfig(ValueError):
    """A test configuration or stored test policy that cannot be used."""


class SnapshotTooLarge(InvalidTestConfig):
    """The label snapshot is over its row bound: a test run is refused."""


@dataclass(frozen=True)
class InvalidPolicy:
    """A stored ``test_config`` that is neither SQL NULL nor a valid policy."""

    reason: str


@dataclass(frozen=True)
class LabelSnapshot:
    """Every label definition, and the admin overrides of the given tools, keyed by tool id."""

    labels: set[str]
    adds: dict[str, set[str]] = field(default_factory=dict)
    removes: dict[str, set[str]] = field(default_factory=dict)


_TEST_MODE_OFF = f"test mode is off in this deployment (set {TEST_MODE_ENV}=1 to allow it)"


def is_test_mode_enabled() -> bool:
    """Whether this deployment allows test runs: only an explicit ``1``."""
    return os.environ.get(TEST_MODE_ENV) == "1"


def require_test_mode() -> None:
    """Refuse a test run where test mode is off (a plain ValueError: a job is not retried)."""
    if not is_test_mode_enabled():
        raise ValueError(_TEST_MODE_OFF)


def parse_test_config(text: str | None) -> dict:
    """Validate the ``--test`` JSON and return it as ``{stub, real}``; ``None`` is ``{}``."""
    from .tool_enablement import declared_tool_names

    if not is_test_mode_enabled():
        raise InvalidTestConfig(_TEST_MODE_OFF)
    text = "{}" if text is None else text
    if len(text.encode("utf-8")) > MAX_TEST_CONFIG_BYTES:
        raise InvalidTestConfig(f"--test is over {MAX_TEST_CONFIG_BYTES} bytes")
    try:
        config = json.loads(text)
    except ValueError:
        raise InvalidTestConfig("--test is not valid JSON") from None
    if not isinstance(config, dict):
        raise InvalidTestConfig("--test must be a JSON object")
    unknown = sorted(set(config) - set(_KEYS))
    if unknown:
        raise InvalidTestConfig(f"--test has unknown keys: {', '.join(unknown)}")

    lists = {}
    for key in _KEYS:
        names = config.get(key, [])
        if not _is_string_list(names):
            raise InvalidTestConfig(f"--test '{key}' must be a list of tool names")
        lists[key] = sorted(set(names))
    both = sorted(set(lists["stub"]) & set(lists["real"]))
    if both:
        raise InvalidTestConfig(f"--test names tools in both 'stub' and 'real': {', '.join(both)}")
    undeclared = sorted((set(lists["stub"]) | set(lists["real"])) - declared_tool_names())
    if undeclared:
        raise InvalidTestConfig(f"--test names tools no module declares: {', '.join(undeclared)}")
    return lists


def _is_string_list(value: object, max_len: int | None = None) -> bool:
    return (isinstance(value, list) and all(isinstance(x, str) for x in value)
            and (max_len is None or len(value) <= max_len))


def _is_v2(value: dict) -> bool:
    return "v" in value


def _v2_problem(value: dict) -> str | None:
    if set(value) != {"v", *_V2_BOUNDS} or value["v"] != POLICY_VERSION:
        return "not a v2 policy"
    bad = [key for key, bound in _V2_BOUNDS.items() if not _is_string_list(value[key], bound)]
    if bad:
        return f"'{bad[0]}' is not a bounded list of strings"
    if set(value["stub"]) & set(value["real"]):
        return "a tool is in both 'stub' and 'real'"
    if not set(value["unstub"]) <= set(value["known"]):
        return "'unstub' names a tool outside 'known'"
    return None


def _legacy_problem(value: dict) -> str | None:
    if not set(value) <= set(_KEYS):
        return "unknown keys"
    if not all(_is_string_list(value[key]) for key in _KEYS if key in value):
        return "a list is not a list of strings"
    if set(value.get("stub", ())) & set(value.get("real", ())):
        return "a tool is in both 'stub' and 'real'"
    return None


def validate_stored_policy(value: object) -> dict:
    """A legacy ``{stub, real}`` or a resolved v2 policy, else ``InvalidTestConfig``.

    The twin of ``validPolicy`` in ``toolbox/capability.js`` (fixture
    ``tests/fixtures/test_policies.json``). The reason never quotes the value.
    """
    if not isinstance(value, dict):
        raise InvalidTestConfig("stored test policy is not an object")
    problem = _v2_problem(value) if _is_v2(value) else _legacy_problem(value)
    if problem:
        raise InvalidTestConfig(f"stored test policy is invalid: {problem}")
    return value


def require_resolved_policy(value: object) -> dict:
    """A valid v2 policy, else ``InvalidTestConfig``: the only form a capability is minted with."""
    policy = validate_stored_policy(value)
    if not _is_v2(policy):
        raise InvalidTestConfig("test policy is not resolved")
    return policy


def capability_envelope(policy: dict) -> list:
    """The capability column form: an array, so a toolbox from before v2 refuses it."""
    return [CAPABILITY_ENVELOPE_TAG, POLICY_VERSION, require_resolved_policy(policy)]


def load_test_config(value) -> dict | InvalidPolicy | None:
    """A stored ``test_config`` column: ``None`` only for SQL NULL. Never raises."""
    if value is None:
        return None
    if isinstance(value, str | bytes):
        try:
            value = json.loads(value)
        except ValueError:
            return InvalidPolicy("stored test policy is not valid JSON")
    try:
        return validate_stored_policy(value)
    except InvalidTestConfig as exc:
        return InvalidPolicy(str(exc))


def tool_key(manifest: ModuleManifest, tool_name: str) -> ToolKey:
    return f"{SOURCE_PREFIX}{manifest.name}", tool_name


def tool_id(key: ToolKey) -> str:
    """``<module>/<tool>``, the id the toolbox builds from its registry entry."""
    source, name = key
    return f"{source.removeprefix(SOURCE_PREFIX)}/{name}"


def manifest_tool_keys(manifests: Iterable[ModuleManifest]) -> list[ToolKey]:
    return [tool_key(m, t["name"]) for m in manifests for t in m.tools
            if isinstance(t, dict) and isinstance(t.get("name"), str)]


def load_label_snapshot(conn, ids: Sequence[ToolKey]) -> LabelSnapshot:
    """Every label definition and the overrides of ``ids``, in one statement.

    The filter is the manifests' tool identity, not ``present``: an override survives a
    disable and re-enable, and the overrides of retired tools never enter the read.
    """
    ids = list(dict.fromkeys(ids))
    if len(ids) > MAX_KNOWN:
        raise SnapshotTooLarge(f"more than {MAX_KNOWN} tools")
    in_ids = (" AND (c.source, c.tool_name) IN (" + ", ".join(["(%s, %s)"] * len(ids)) + ")"
              if ids else " AND FALSE")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT l.label_key, c.source, c.tool_name, a.mode "
            "FROM tool_label l LEFT JOIN (tool_label_assignment a JOIN tool_catalog c "
            f"ON c.id = a.tool_id{in_ids}) ON a.label_id = l.id "
            "LIMIT %s",
            (*[part for key in ids for part in key], MAX_SNAPSHOT_ROWS + 1),
        )
        rows = cur.fetchall()
    if len(rows) > MAX_SNAPSHOT_ROWS:
        raise SnapshotTooLarge(f"the label snapshot is over {MAX_SNAPSHOT_ROWS} rows")
    snapshot = LabelSnapshot(labels=set())
    for r in rows:
        snapshot.labels.add(r["label_key"])
        if r["mode"] in ("add", "remove"):
            overrides = snapshot.adds if r["mode"] == "add" else snapshot.removes
            overrides.setdefault(r["label_key"], set()).add(tool_id((r["source"], r["tool_name"])))
    return snapshot


_DEFAULTS_SLOT: list[tuple[int, list[ModuleManifest], dict[str, frozenset[str]]]] = []


def default_labels_by_tool(manifests: Iterable[ModuleManifest]) -> dict[str, frozenset[str]]:
    """``{<module>/<tool>: manifest default labels}``, the labels the toolbox registry holds.

    Kept once per ``bootstrap()`` for the same manifest objects (one slot, SCL-1): a claim
    reads it, and the config.json defaults freeze with the manifests until the next
    bootstrap, as ``modules_changed_since_bootstrap`` reports. Callers treat it as read-only.
    """
    from .bootstrap import bootstrapped_at

    manifests = list(manifests)
    generation = bootstrapped_at()
    if _DEFAULTS_SLOT and _is_same_bootstrap(_DEFAULTS_SLOT[0], generation, manifests):
        return _DEFAULTS_SLOT[0][2]
    config_defaults: dict = {}
    for m in manifests:
        config_defaults.update(read_config_defaults(m.path))
    defaults = {
        tool_id(tool_key(m, t["name"])): default_labels(t, config_defaults, in_tree=is_in_tree(m))
        for m in manifests for t in m.tools
        if isinstance(t, dict) and isinstance(t.get("name"), str)
    }
    if generation is not None:
        _DEFAULTS_SLOT[:] = [(generation, manifests, defaults)]
    return defaults


def _is_same_bootstrap(slot: tuple, generation: int | None, manifests: list[ModuleManifest]) -> bool:
    cached_generation, cached_manifests, _ = slot
    return (cached_generation == generation and len(cached_manifests) == len(manifests)
            and all(a is b for a, b in zip(cached_manifests, manifests, strict=True)))


def effective_labels(manifests: Iterable[ModuleManifest], snapshot: LabelSnapshot, *,
                     defaults: Mapping[str, frozenset[str]] | None = None) -> dict[str, frozenset[str]]:
    """``{<module>/<tool>: defaults, plus admin adds, minus admin removes}``.

    ``defaults`` is ``default_labels_by_tool(manifests)`` when the caller already has it.
    """
    def overridden(tid: str, overrides: Mapping[str, set[str]]) -> set[str]:
        return {label for label, ids in overrides.items() if tid in ids}

    return {
        tid: frozenset((labels | overridden(tid, snapshot.adds)) - overridden(tid, snapshot.removes))
        for tid, labels in (default_labels_by_tool(manifests) if defaults is None else defaults).items()
    }


def resolve_test_policy(config: dict, snapshot: LabelSnapshot, manifests: Iterable[ModuleManifest], *,
                        stub_labels: Sequence[str] = ()) -> dict:
    """Resolve a legacy ``{stub, real}`` (+ ``--stub-tools`` labels) into a v2 policy.

    An already resolved v2 policy is returned unchanged: a child or a retry never re-resolves.
    """
    config = validate_stored_policy(config)
    if _is_v2(config):
        return config
    wanted = sorted(set(stub_labels))
    unknown = [label for label in wanted if label not in snapshot.labels]
    if unknown:
        raise InvalidTestConfig(f"--stub-tools names an unknown label: {', '.join(unknown)}")
    real = set(config.get("real", ()))
    stub = set(config.get("stub", ()))
    unstub = set()
    labels = effective_labels(manifests, snapshot)
    removed_defaults = snapshot.removes.get(STUB_IN_TEST, set())
    for tid, tool_labels in labels.items():
        name = tid.partition("/")[2]
        matched = tool_labels & set(wanted)
        if matched and name in real:
            raise InvalidTestConfig(
                f"tool '{name}' has label '{sorted(matched)[0]}' given to --stub-tools and is in 'real'")
        if matched or (STUB_IN_TEST in tool_labels and name not in real):
            stub.add(name)
    for tid in labels:
        if tid in removed_defaults and tid.partition("/")[2] not in stub:
            unstub.add(tid)
    if len(stub) > MAX_STUBBED:
        raise InvalidTestConfig(f"the test policy stubs more than {MAX_STUBBED} tools")
    if len(labels) > MAX_KNOWN:
        raise InvalidTestConfig(f"the test policy covers more than {MAX_KNOWN} tools")
    return require_resolved_policy({
        "v": POLICY_VERSION, "stub": sorted(stub), "real": sorted(real), "stub_labels": wanted,
        "unstub": sorted(unstub), "known": sorted(labels),
    })


def resolve_run_policy(conn, config: dict, *, stub_labels: Sequence[str] = (),
                       manifests: Iterable[ModuleManifest] | None = None) -> dict:
    """Read one label snapshot for the bootstrapped manifests and resolve ``config``."""
    if manifests is None:
        from .bootstrap import get_manifests

        manifests = get_manifests()
    manifests = list(manifests)
    snapshot = load_label_snapshot(conn, manifest_tool_keys(manifests))
    return resolve_test_policy(config, snapshot, manifests, stub_labels=stub_labels)


def is_stubbed(policy: dict | None, tool_id_: str, default_labels_: frozenset[str]) -> bool | None:
    """The dispatcher's decision (``stubDecision`` in toolbox/dispatcher.js) for one tool.

    ``True`` stub, ``False`` run, ``None`` refuse. ``default_labels_`` are the manifest
    defaults the toolbox registry holds, never the admin overrides.
    """
    if policy is None:
        return False
    name = tool_id_.partition("/")[2]
    stub_in_test_default = STUB_IN_TEST in default_labels_
    if not _is_v2(policy):
        return name in policy.get("stub", ()) or (stub_in_test_default and name not in policy.get("real", ()))
    if tool_id_ not in policy["known"]:
        return None
    if name in policy["stub"]:
        return True
    if stub_in_test_default and name not in policy["real"] and tool_id_ not in policy["unstub"]:
        return None
    return False
