"""The test policy: public input, resolution to v2, the stored forms (framework/test_run.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.framework.module_loader import ModuleManifest
from agento.framework.retry_policy import NON_RETRYABLE_ERRORS
from agento.framework.test_run import (
    MAX_STUBBED,
    MAX_TEST_CONFIG_BYTES,
    TEST_MODE_ENV,
    InvalidPolicy,
    InvalidTestConfig,
    LabelSnapshot,
    SnapshotTooLarge,
    capability_envelope,
    default_labels_by_tool,
    effective_labels,
    is_stubbed,
    is_test_mode_enabled,
    load_label_snapshot,
    load_test_config,
    parse_test_config,
    require_resolved_policy,
    require_test_mode,
    resolve_test_policy,
    validate_stored_policy,
)
from agento.framework.tool_catalog import BUILTIN_LABEL_KEYS, STUB_IN_TEST

FIXTURE = json.loads((Path(__file__).parents[2] / "fixtures" / "test_policies.json").read_text())


@pytest.fixture(autouse=True)
def enable_test_mode(monkeypatch):
    monkeypatch.setenv(TEST_MODE_ENV, "1")


def _manifest(tmp_path, name: str, tools: dict[str, list[str]]) -> ModuleManifest:
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return ModuleManifest(name=name, version="1.0.0", description="", path=path,
                          tools=[{"name": t, "labels": labels} for t, labels in tools.items()])


def _snapshot(*custom: str, adds=None, removes=None) -> LabelSnapshot:
    keys = (*BUILTIN_LABEL_KEYS, *custom)
    return LabelSnapshot(labels=set(keys),
                         adds=adds or {}, removes=removes or {})


@pytest.mark.parametrize(("value", "enabled"), [
    ("1", True), (None, False), ("", False), ("0", False), ("true", False),
])
def test_test_mode_is_on_only_for_an_explicit_1(monkeypatch, value, enabled):
    if value is None:
        monkeypatch.delenv(TEST_MODE_ENV)
    else:
        monkeypatch.setenv(TEST_MODE_ENV, value)
    assert is_test_mode_enabled() is enabled


def test_with_test_mode_off_a_config_is_refused_and_a_job_is_not_retried(monkeypatch):
    monkeypatch.delenv(TEST_MODE_ENV)
    with pytest.raises(InvalidTestConfig, match="test mode is off"):
        parse_test_config("{}")
    with pytest.raises(ValueError, match=TEST_MODE_ENV) as exc:
        require_test_mode()
    # The consumer reads the class name: a plain ValueError fails the job at once.
    assert type(exc.value).__name__ in NON_RETRYABLE_ERRORS


@pytest.mark.parametrize("error", [InvalidTestConfig, SnapshotTooLarge])
def test_a_policy_error_is_not_retried(error):
    assert error.__name__ in NON_RETRYABLE_ERRORS


@pytest.mark.parametrize(("text", "parsed"), [
    (None, {"stub": [], "real": []}),
    ("{}", {"stub": [], "real": []}),
    ('{"stub": ["jira_search", "jira_search"], "real": ["jira_add_comment"]}',
     {"stub": ["jira_search"], "real": ["jira_add_comment"]}),
])
def test_a_valid_config_is_returned_in_canonical_form(text, parsed):
    assert parse_test_config(text) == parsed


@pytest.mark.parametrize(("text", "message"), [
    ("{not json", "not valid JSON"),
    ("[]", "must be a JSON object"),
    ('{"stub": [], "ssh": true}', "unknown keys: ssh"),
    ('{"stub": "jira_search"}', "'stub' must be a list of tool names"),
    ('{"real": [1]}', "'real' must be a list of tool names"),
    ('{"stub": ["jira_search"], "real": ["jira_search"]}', "both 'stub' and 'real': jira_search"),
    ('{"stub": ["no_such_tool"]}', "no module declares: no_such_tool"),
    ('{"stub": ["' + "x" * MAX_TEST_CONFIG_BYTES + '"]}', "over"),
    ('{"v": 2}', "unknown keys: v"),
    ('{"unstubbed": []}', "unknown keys: unstubbed"),
    ('{"stub_labels": ["finanse"]}', "unknown keys: stub_labels"),
], ids=["json", "object", "key", "stub-list", "real-list", "both", "undeclared", "size",
        "internal-v", "internal-unstubbed", "internal-stub-labels"])
def test_an_invalid_config_is_refused(text, message):
    with pytest.raises(InvalidTestConfig, match=message):
        parse_test_config(text)


@pytest.mark.parametrize("case", FIXTURE["stored"], ids=lambda c: c["name"])
def test_policy_fixture(case):
    value = case["value"]
    if case["valid"]:
        assert validate_stored_policy(value) == value
        assert load_test_config(json.dumps(value)) == value
    else:
        with pytest.raises(InvalidTestConfig):
            validate_stored_policy(value)
        assert isinstance(load_test_config(json.dumps(value)), InvalidPolicy)
    if case["valid"] and case["v2"]:
        assert require_resolved_policy(value) == value
    else:
        with pytest.raises(InvalidTestConfig):
            require_resolved_policy(value)


def _v2(**overrides) -> dict:
    return {"v": 2, "stub": [], "real": [], "stub_labels": [], "unstub": [], "known": [], **overrides}


@pytest.mark.parametrize(("value", "valid"), [
    (_v2(stub=[f"t{i}" for i in range(MAX_STUBBED)]), True),
    (_v2(stub=[f"t{i}" for i in range(MAX_STUBBED + 1)]), False),
    (_v2(stub_labels=[f"l{i}" for i in range(201)]), False),
    (_v2(known=[f"m/t{i}" for i in range(2001)]), False),
    (_v2(v=2.0), True),
    (_v2(v=True), False),
    ({"stub": [f"t{i}" for i in range(5000)]}, True),
], ids=["stub-at-bound", "stub-over", "labels-over", "known-over", "v-float", "v-bool", "legacy-unbounded"])
def test_stored_policy_bounds(value, valid):
    if valid:
        assert validate_stored_policy(value) is value
    else:
        with pytest.raises(InvalidTestConfig):
            validate_stored_policy(value)


@pytest.mark.parametrize("stored", [
    "null", "[]", "1", "{not json", '{"v": 3}', '{"stubbed": ["email_send"]}', '{"stub": [1]}',
    json.dumps(_v2(extra=[])),
])
def test_load_test_config_strict(stored):
    loaded = load_test_config(stored)
    assert isinstance(loaded, InvalidPolicy)
    assert "email_send" not in loaded.reason


def test_load_test_config_none_only_for_sql_null():
    assert load_test_config(None) is None
    assert load_test_config({"stub": ["a"], "real": []}) == {"stub": ["a"], "real": []}


def test_the_capability_envelope_wraps_only_a_resolved_policy():
    assert capability_envelope(_v2()) == ["agento-test-policy", 2, _v2()]
    with pytest.raises(InvalidTestConfig):
        capability_envelope({"stub": [], "real": []})


class TestResolve:
    def _manifests(self, tmp_path):
        return [_manifest(tmp_path, "m", {"a": [STUB_IN_TEST], "b": [], "c": []})]

    @pytest.mark.parametrize(("real", "stub"), [([], ["a", "b", "c"]), (["a"], ["b", "c"])])
    def test_resolve_union_and_real_subtraction(self, tmp_path, real, stub):
        snapshot = _snapshot("finanse", adds={"finanse": {"m/b"}})
        policy = resolve_test_policy({"stub": ["c"], "real": real}, snapshot, self._manifests(tmp_path),
                                     stub_labels=["finanse"])
        assert policy == {"v": 2, "stub": stub, "real": real, "stub_labels": ["finanse"],
                          "unstub": [], "known": ["m/a", "m/b", "m/c"]}

    def test_override_remove_stub_in_test_goes_to_unstub(self, tmp_path):
        snapshot = _snapshot(removes={STUB_IN_TEST: {"m/a"}})
        policy = resolve_test_policy({}, snapshot, self._manifests(tmp_path))
        assert policy["stub"] == []
        assert policy["unstub"] == ["m/a"]

    def test_retired_removal_does_not_cover_other_module_same_name(self, tmp_path):
        manifests = [_manifest(tmp_path, "other", {"a": [STUB_IN_TEST]})]
        snapshot = _snapshot(removes={STUB_IN_TEST: {"m/a"}})
        policy = resolve_test_policy({}, snapshot, manifests)
        assert policy["stub"] == ["a"]
        assert policy["unstub"] == []

    def test_stub_label_match_in_real_is_error(self, tmp_path):
        snapshot = _snapshot("finanse", adds={"finanse": {"m/b"}})
        with pytest.raises(InvalidTestConfig, match="tool 'b'"):
            resolve_test_policy({"real": ["b"]}, snapshot, self._manifests(tmp_path), stub_labels=["finanse"])

    def test_unknown_label_is_error(self, tmp_path):
        with pytest.raises(InvalidTestConfig, match="unknown label: nope"):
            resolve_test_policy({}, _snapshot(), self._manifests(tmp_path), stub_labels=["nope"])

    @pytest.mark.parametrize("label", ["empty-custom", "private-data"])
    def test_known_label_without_tools_adds_nothing(self, tmp_path, label):
        policy = resolve_test_policy({}, _snapshot("empty-custom"), self._manifests(tmp_path),
                                     stub_labels=[label])
        assert policy["stub"] == ["a"]

    def test_a_resolved_policy_is_returned_unchanged(self, tmp_path):
        resolved = _v2(stub=["x"], known=["m/x"])
        assert resolve_test_policy(resolved, _snapshot(), self._manifests(tmp_path)) is resolved

    def test_too_many_stubbed_tools_is_error(self, tmp_path):
        manifests = [_manifest(tmp_path, "m", {f"t{i}": [STUB_IN_TEST] for i in range(MAX_STUBBED + 1)})]
        with pytest.raises(InvalidTestConfig, match="more than"):
            resolve_test_policy({}, _snapshot(), manifests)

    def test_effective_labels_are_defaults_plus_adds_minus_removes(self, tmp_path):
        snapshot = _snapshot("finanse", adds={"finanse": {"m/b"}}, removes={STUB_IN_TEST: {"m/a"}})
        assert effective_labels(self._manifests(tmp_path), snapshot) == {
            "m/a": frozenset(), "m/b": frozenset({"finanse"}), "m/c": frozenset()}


def test_default_labels_are_kept_per_bootstrap(tmp_path, monkeypatch):
    """A claim reuses the defaults of its bootstrap; a new bootstrap or other manifests re-read them."""
    from agento.framework import bootstrap

    def sensitive(value: str) -> None:
        (tmp_path / "m" / "config.json").write_text(json.dumps({"tools/a/sensitive": value}))

    manifests = [_manifest(tmp_path, "m", {"a": []})]
    sensitive("1")
    monkeypatch.setattr(bootstrap, "bootstrapped_at", lambda: 1)
    first = default_labels_by_tool(manifests)
    assert first == {"m/a": frozenset({STUB_IN_TEST})}

    sensitive("0")
    assert default_labels_by_tool(list(manifests)) is first
    assert default_labels_by_tool([_manifest(tmp_path, "m", {"a": []})]) == {"m/a": frozenset()}

    monkeypatch.setattr(bootstrap, "bootstrapped_at", lambda: 2)
    assert default_labels_by_tool(manifests) == {"m/a": frozenset()}


_POLICY = _v2(stub=["s"], real=["r"], unstub=["m/u"], known=["m/s", "m/r", "m/u", "m/d", "m/n"])
_DEFAULT = frozenset({STUB_IN_TEST})


@pytest.mark.parametrize(("policy", "tool", "defaults", "decision"), [
    (None, "m/d", _DEFAULT, False),
    ({"stub": ["s"], "real": []}, "m/s", frozenset(), True),
    ({"stub": [], "real": []}, "m/d", _DEFAULT, True),
    ({"stub": [], "real": ["d"]}, "m/d", _DEFAULT, False),
    (_POLICY, "m/x", frozenset(), None),
    (_POLICY, "other/s", frozenset(), None),
    (_POLICY, "m/s", frozenset(), True),
    (_POLICY, "m/n", frozenset(), False),
    (_POLICY, "m/d", _DEFAULT, None),
    (_POLICY, "m/u", _DEFAULT, False),
    (_POLICY, "m/r", _DEFAULT, False),
], ids=["production", "legacy-stub", "legacy-default", "legacy-real", "v2-unknown", "v2-other-module",
        "v2-stub", "v2-run", "v2-new-default", "v2-unstub", "v2-real"])
def test_is_stubbed_mirrors_the_dispatcher(policy, tool, defaults, decision):
    assert is_stubbed(policy, tool, defaults) is decision


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def execute(self, sql, args=()):
        self.calls.append((sql, args))

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.cur = _Cursor(rows)

    def cursor(self):
        return self.cur


def test_snapshot_is_one_statement():
    rows = [
        {"label_key": "finanse", "name": "Finanse", "is_protected": 0, "source": "module:m",
         "tool_name": "b", "mode": "add"},
        {"label_key": STUB_IN_TEST, "name": "Stub in test", "is_protected": 1, "source": "module:m",
         "tool_name": "a", "mode": "remove"},
        {"label_key": "empty", "name": "Empty", "is_protected": 0, "source": None, "tool_name": None, "mode": None},
    ]
    conn = _Conn(rows)
    snapshot = load_label_snapshot(conn, [("module:m", "a"), ("module:m", "b")])
    assert len(conn.cur.calls) == 1
    assert set(snapshot.labels) == {"finanse", STUB_IN_TEST, "empty"}
    assert snapshot.adds == {"finanse": {"m/b"}}
    assert snapshot.removes == {STUB_IN_TEST: {"m/a"}}


def test_snapshot_overflow_refuses_test_run(monkeypatch):
    from agento.framework import test_run

    monkeypatch.setattr(test_run, "MAX_SNAPSHOT_ROWS", 1)
    row = {"label_key": "x", "name": "x", "is_protected": 0, "source": None, "tool_name": None, "mode": None}
    with pytest.raises(SnapshotTooLarge):
        load_label_snapshot(_Conn([row, {**row, "label_key": "y"}]), [])
