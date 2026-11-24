"""The tool catalog: manifest entries, review fingerprint, sync and effective labels."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.framework import tool_catalog
from agento.framework.access.accounts import AccessError
from agento.framework.module_loader import ModuleManifest
from agento.framework.tool_catalog import (
    BUILTIN_LABEL_KEYS,
    MAX_LABELS,
    STUB_IN_TEST,
    CatalogConflict,
    SyncResult,
    classify_tool,
    create_label,
    default_labels,
    delete_label,
    list_labels,
    load_catalog,
    manifest_entries,
    module_fingerprint,
    rename_label,
    review_fingerprints,
    sync_catalog,
    sync_enabled_modules,
)

SQL_DIR = Path(__file__).resolve().parents[3] / "src" / "agento" / "framework" / "sql"


class FakeCatalogDb:
    """The tool_catalog / tool_label_assignment rows, served to the statements tool_catalog runs."""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.assignments: list[tuple[int, str, str]] = []
        self.config_rows: list[dict] = []
        self.writes: list[str] = []
        self.commits = 0
        self._result: list[dict] = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def fetchall(self):
        return self._result

    def execute(self, sql, params=()):
        if sql.startswith("SELECT path, scope, scope_id, value FROM core_config_data"):
            self._result = [r for r in self.config_rows if r["path"] in params]
        elif sql.startswith("SELECT id, source, tool_name"):
            self._result = [dict(r) for r in self.rows.values() if r["source"].startswith("module:")]
        elif sql.startswith("SELECT c.id"):
            self._result = self._joined()
        elif sql.startswith("UPDATE tool_catalog SET present = 0"):
            self.writes.append("absent")
            for tool_id in params:
                self.rows[tool_id]["present"] = 0
        else:
            raise AssertionError(f"unexpected statement: {sql}")

    def executemany(self, sql, seq):
        for params in seq:
            if sql.startswith("INSERT INTO tool_catalog"):
                self.writes.append("insert")
                source, tool_name, alias, toolset, description, labels, contract = params
                tool_id = len(self.rows) + 1
                self.rows[tool_id] = {
                    "id": tool_id, "source": source, "tool_name": tool_name, "alias": alias,
                    "toolset": toolset, "description": description, "default_labels": labels,
                    "contract_hash": contract, "reviewed_hash": None, "revision": 0, "present": 1,
                }
            elif sql.startswith("UPDATE tool_catalog SET"):
                self.writes.append("update")
                alias, toolset, description, labels, bump, contract, tool_id = params
                self.rows[tool_id].update(
                    alias=alias, toolset=toolset, description=description, default_labels=labels,
                    revision=self.rows[tool_id]["revision"] + bump, contract_hash=contract, present=1,
                )
            else:
                raise AssertionError(f"unexpected statement: {sql}")

    def _joined(self):
        out = []
        for row in self.rows.values():
            if not row["present"]:
                continue
            mine = [(key, mode) for tool_id, key, mode in self.assignments if tool_id == row["id"]]
            for key, mode in mine or [(None, None)]:
                out.append({**row, "label_key": key, "mode": mode})
        return out


def _manifest(tmp_path, name, tools, *, config=None, system=None, lock=None, version="1.0.0"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (d / "config.json").write_text(json.dumps(config))
    if lock is not None:
        (d / "tool-contracts.json").write_text(json.dumps(lock))
    return ModuleManifest(name=name, version=version, description="", path=d, tools=tools, config=system or {})


def _tool(name, **extra):
    return {"type": "mcp", "name": name, "description": f"{name} tool", "toolset": "demo", **extra}


def _entries(manifests, *, in_tree=True, config_defaults=None, fingerprints=None):
    return manifest_entries(manifests, config_defaults or {}, in_tree=lambda _m: in_tree,
                            fingerprints=fingerprints)


def _by_name(db):
    return {r["tool_name"]: r for r in db.rows.values()}


def test_sync_inserts_and_marks_absent(tmp_path):
    db = FakeCatalogDb()
    a = _manifest(tmp_path, "a", [_tool("a_one"), _tool("a_two")])
    assert sync_catalog(db, _entries([a])) == SyncResult(inserted=2, changed=0, absent=0)
    a.tools.pop()
    assert sync_catalog(db, _entries([a])) == SyncResult(inserted=0, changed=0, absent=1)
    assert _by_name(db)["a_two"]["present"] == 0
    assert _by_name(db)["a_one"]["present"] == 1


def test_removed_module_marked_absent(tmp_path):
    db = FakeCatalogDb()
    a = _manifest(tmp_path, "a", [_tool("a_one")])
    b = _manifest(tmp_path, "b", [_tool("b_one")])
    sync_catalog(db, _entries([a, b]))
    sync_catalog(db, _entries([a]))
    assert _by_name(db)["b_one"]["present"] == 0


def test_resync_same_manifest_writes_nothing(tmp_path):
    db = FakeCatalogDb()
    a = _manifest(tmp_path, "a", [_tool("a_one", labels=["private-data"]), _tool("a_two", toolset=None)])
    sync_catalog(db, _entries([a]))
    db.writes.clear()
    assert sync_catalog(db, _entries([a])) == SyncResult(inserted=0, changed=0, absent=0)
    assert db.writes == []


def test_contract_change_resets_review_bumps_revision_keeps_assignments(tmp_path):
    db = FakeCatalogDb()
    a = _manifest(tmp_path, "a", [_tool("a_one")], lock={"a_one": "sha-1"})
    sync_catalog(db, _entries([a]))
    row = _by_name(db)["a_one"]
    row["reviewed_hash"] = row["contract_hash"]
    db.assignments.append((row["id"], "private-data", "add"))
    (a.path / "tool-contracts.json").write_text(json.dumps({"a_one": "sha-2"}))
    assert sync_catalog(db, _entries([a])).changed == 1
    [tool] = load_catalog(db, manifests=[a])
    assert (tool.reviewed, tool.revision, tool.labels) == (False, 1, frozenset({"private-data"}))


def test_absent_then_rediscovered_keeps_assignments(tmp_path):
    db = FakeCatalogDb()
    a = _manifest(tmp_path, "a", [_tool("a_one")])
    sync_catalog(db, _entries([a]))
    db.assignments.append((1, "untrusted-content", "add"))
    sync_catalog(db, _entries([]))
    assert load_catalog(db, manifests=[a]) == []
    sync_catalog(db, _entries([a]))
    [tool] = load_catalog(db, manifests=[a])
    assert tool.present and "untrusted-content" in tool.labels


def test_tool_name_case_sensitive(tmp_path):
    db = FakeCatalogDb()
    a = _manifest(tmp_path, "a", [_tool("a_one"), _tool("A_one")])
    assert sync_catalog(db, _entries([a])).inserted == 2
    ddl = (SQL_DIR / "060_tool_catalog.sql").read_text()
    assert "tool_name VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL" in ddl


@pytest.mark.parametrize("in_tree", [True, False])
@pytest.mark.parametrize(("sensitive", "expected"), [
    ("1", {STUB_IN_TEST}), ("0", set()), (None, set()),
])
def test_sensitive_translation(sensitive, expected, in_tree):
    defaults = {} if sensitive is None else {"tools/t1/sensitive": sensitive}
    assert default_labels(_tool("t1"), defaults, in_tree=in_tree) == expected


def test_out_of_tree_risk_labels_ignored():
    tool = _tool("t1", labels=["private-data", STUB_IN_TEST, "external-communication"])
    assert default_labels(tool, {}, in_tree=False) == {STUB_IN_TEST}
    assert default_labels(tool, {}, in_tree=True) == {"private-data", STUB_IN_TEST, "external-communication"}


def test_effective_labels_default_plus_add_minus_remove(tmp_path):
    db = FakeCatalogDb()
    a = _manifest(tmp_path, "a", [_tool("a_one", labels=["private-data", STUB_IN_TEST], requires="a_master"),
                                  _tool("a_master")])
    sync_catalog(db, _entries([a]))
    tool_id = _by_name(db)["a_one"]["id"]
    db.assignments += [(tool_id, "finanse", "add"), (tool_id, STUB_IN_TEST, "remove")]
    tool = next(t for t in load_catalog(db, manifests=[a]) if t.tool_name == "a_one")
    assert tool.labels == {"private-data", "finanse"}
    assert (tool.source, tool.alias, tool.requires) == ("module:a", "a_one", "a_master")


def _contract(tmp_path, *, rows=(), env=None, file_default="https://a.example.com", version="1.0.0"):
    system = {"host": {"type": "string", "review_fingerprint": True}, "other": {"type": "string"}}
    m = _manifest(tmp_path, "jira_like", [_tool("jl_one")], config={"host": file_default},
                  system=system, version=version)
    db = FakeCatalogDb()
    db.config_rows = [{"path": "jira_like/host", "scope": s, "scope_id": i, "value": v} for s, i, v in rows]
    environ = {} if env is None else {"CONFIG__JIRA_LIKE__HOST": env}
    fingerprints = review_fingerprints(db, [m], environ=environ)
    [entry] = manifest_entries([m], {}, in_tree=lambda _m: False, fingerprints=fingerprints)
    return entry.contract_hash, fingerprints


@pytest.mark.parametrize("change", [
    {"rows": [("agent_view", 3, "https://b.example.com")]},
    {"env": "https://c.example.com"},
    {"file_default": "https://d.example.com"},
])
def test_target_config_change_resets_review(tmp_path, change):
    db = FakeCatalogDb()
    before, _ = _contract(tmp_path / "1")
    after, _ = _contract(tmp_path / "2", **change)
    assert before != after
    m = _manifest(tmp_path / "3", "jira_like", [_tool("jl_one")])
    first = manifest_entries([m], {}, in_tree=lambda _m: False, fingerprints={"jira_like": "x"})
    sync_catalog(db, first)
    second = manifest_entries([m], {}, in_tree=lambda _m: False, fingerprints={"jira_like": "y"})
    assert sync_catalog(db, second).changed == 1
    assert _by_name(db)["jl_one"]["revision"] == 1


def test_unflagged_field_change_keeps_review(tmp_path):
    _, before = _contract(tmp_path / "1")
    m = _manifest(tmp_path / "2", "jira_like", [_tool("jl_one")], config={"host": "https://a.example.com"},
                  system={"host": {"type": "string", "review_fingerprint": True}, "other": {"type": "string"}})
    db = FakeCatalogDb()
    db.config_rows = [{"path": "jira_like/other", "scope": "default", "scope_id": 0, "value": "changed"}]
    assert review_fingerprints(db, [m], environ={}) == before


def test_masked_db_edit_keeps_review(tmp_path):
    a, _ = _contract(tmp_path / "1", env="https://c.example.com")
    b, _ = _contract(tmp_path / "2", env="https://c.example.com", rows=[("default", 0, "https://z.example.com")])
    assert a == b


def test_out_of_tree_version_change_resets_review(tmp_path):
    a, _ = _contract(tmp_path / "1", version="1.0.0")
    b, _ = _contract(tmp_path / "2", version="1.0.1")
    assert a != b


def test_lock_value_replaces_version(tmp_path):
    m1 = _manifest(tmp_path / "1", "a", [_tool("a_one")], lock={"a_one": "sha-1"}, version="1.0.0")
    m2 = _manifest(tmp_path / "2", "a", [_tool("a_one")], lock={"a_one": "sha-1"}, version="2.0.0")
    assert _entries([m1])[0].contract_hash == _entries([m2])[0].contract_hash


def test_fingerprint_never_holds_value(tmp_path):
    secretish = "https://tenant-name.example.com"
    contract, fingerprints = _contract(tmp_path, rows=[("default", 0, secretish)], env=None, file_default=secretish)
    assert secretish not in json.dumps(fingerprints)
    assert secretish not in contract
    assert module_fingerprint({"jira_like/host": {"env": secretish}}) != ""
    assert secretish not in module_fingerprint({"jira_like/host": {"env": secretish}})


def test_review_fingerprints_is_one_query(tmp_path):
    m = _manifest(tmp_path, "jira_like", [_tool("jl_one")],
                  system={"host": {"type": "string", "review_fingerprint": True},
                          "tenant": {"type": "string", "review_fingerprint": True}})
    db = FakeCatalogDb()
    calls = []
    original = db.execute
    db.execute = lambda sql, params=(): (calls.append(sql), original(sql, params))[1]
    review_fingerprints(db, [m], environ={})
    assert len(calls) == 1


def test_sync_enabled_modules_refuses_empty_module_set(tmp_path):
    db = FakeCatalogDb()
    with pytest.raises(RuntimeError, match="no enabled module"):
        sync_enabled_modules(db, core_dir=str(tmp_path / "none"), user_dir=str(tmp_path / "none2"))
    assert db.writes == []


BUILTIN_LABELS = [
    ("private-data", "Private data access"), ("untrusted-content", "Untrusted content"),
    ("external-communication", "External communication"), ("stub-in-test", "Stub in test"),
]


class FakeLabelDb:
    """tool_label, tool_label_assignment and tool_catalog as MySQL keeps them: unique keys raise
    IntegrityError, a label delete cascades to its assignments, rollback restores the begin state."""

    def __init__(self):
        self.labels: dict[int, dict] = {}
        self.tools: dict[int, dict] = {}
        self.assignments: dict[tuple[int, int], str] = {}
        self.commits = self.rollbacks = 0
        self.fail_on: str | None = None
        self.locked: list[str] = []
        self._saved = None
        self._result: list[dict] = []
        self.lastrowid = 0
        for key, name in BUILTIN_LABELS:
            self._insert_label(key, name, name.casefold(), protected=1)

    def add_tool(self, name, default=(), *, present=1):
        tool_id = len(self.tools) + 1
        self.tools[tool_id] = {"id": tool_id, "source": "module:demo", "tool_name": name,
                               "default_labels": json.dumps(sorted(default)), "contract_hash": "c" * 40,
                               "reviewed_hash": None, "revision": 0, "present": present}
        return tool_id

    def key_of(self, label_id):
        return self.labels[label_id]["label_key"]

    def overrides(self, tool_id):
        return {(self.key_of(label_id), mode) for (t, label_id), mode in self.assignments.items() if t == tool_id}

    def _insert_label(self, key, name, norm, protected=0):
        import pymysql

        if any(r["label_key"] == key or r["name_norm"] == norm for r in self.labels.values()):
            raise pymysql.err.IntegrityError(1062, "Duplicate entry")
        self.lastrowid = max(self.labels, default=0) + 1
        self.labels[self.lastrowid] = {"id": self.lastrowid, "label_key": key, "name": name,
                                       "name_norm": norm, "is_protected": protected}

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def begin(self):
        import copy

        self._saved = copy.deepcopy((self.labels, self.tools, self.assignments))

    def commit(self):
        self.commits += 1
        self._saved = None

    def rollback(self):
        self.rollbacks += 1
        if self._saved is not None:
            self.labels, self.tools, self.assignments = self._saved
            self._saved = None

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def executemany(self, sql, seq):
        for params in seq:
            self.execute(sql, params)

    def execute(self, sql, params=()):
        if self.fail_on and sql.startswith(self.fail_on):
            raise RuntimeError("injected failure")
        if "FOR UPDATE" in sql:
            self.locked.append(sql.split(" FROM ")[1].split()[0])
        if sql.startswith("SELECT l.id, l.label_key"):
            self._result = list(self.labels.values())
        elif sql.startswith("SELECT c.id, c.default_labels"):
            self._result = [{"id": tool["id"], "default_labels": tool["default_labels"], "label_id": label_id,
                             "mode": mode}
                            for tool in self.tools.values() if tool["present"]
                            for label_id, mode in ([(lid, m) for (tid, lid), m in self.assignments.items()
                                                    if tid == tool["id"]] or [(None, None)])]
        elif sql.startswith("SELECT COUNT(*) AS n FROM tool_label"):
            self._result = [{"n": len(self.labels)}]
        elif sql.startswith("SELECT id, label_key, name, is_protected FROM tool_label WHERE name_norm"):
            self._result = [r for r in self.labels.values() if r["name_norm"] == params[0]]
        elif sql.startswith("SELECT id FROM tool_label WHERE name_norm"):
            self._result = [r for r in self.labels.values() if r["name_norm"] == params[0] and r["id"] != params[1]]
        elif sql.startswith("SELECT label_key FROM tool_label WHERE label_key = %s OR label_key LIKE"):
            prefix = params[1].removesuffix("%")
            self._result = [r for r in self.labels.values()
                            if r["label_key"] == params[0] or r["label_key"].startswith(prefix)]
        elif sql.startswith("SELECT id, label_key FROM tool_label WHERE label_key IN"):
            self._result = [r for r in self.labels.values() if r["label_key"] in params]
        elif sql.startswith("SELECT id, is_protected FROM tool_label WHERE id"):
            self._result = [r for r in self.labels.values() if r["id"] == params[0]]
        elif sql.startswith("INSERT INTO tool_label ("):
            self._insert_label(*params)
        elif sql.startswith("UPDATE tool_label SET name"):
            name, norm, label_id = params
            if any(r["name_norm"] == norm and r["id"] != label_id for r in self.labels.values()):
                import pymysql

                raise pymysql.err.IntegrityError(1062, "Duplicate entry")
            self.labels[label_id].update(name=name, name_norm=norm)
        elif sql.startswith("DELETE FROM tool_label WHERE id"):
            self.labels.pop(params[0], None)
            self.assignments = {k: v for k, v in self.assignments.items() if k[1] != params[0]}
        elif sql.startswith("SELECT id, source, default_labels"):
            self._result = [dict(self.tools[params[0]])] if params[0] in self.tools else []
        elif sql.startswith("DELETE FROM tool_label_assignment WHERE tool_id"):
            self.assignments = {k: v for k, v in self.assignments.items() if k[0] != params[0]}
        elif sql.startswith("INSERT INTO tool_label_assignment"):
            tool_id, label_id, mode = params
            assert label_id in self.labels, "FK"
            self.assignments[(tool_id, label_id)] = mode
        elif sql.startswith("UPDATE tool_catalog SET"):
            tool = self.tools[params[0]]
            if "reviewed_hash = contract_hash" in sql:
                tool["reviewed_hash"] = tool["contract_hash"]
            elif "reviewed_hash = NULL" in sql:
                tool["reviewed_hash"] = None
            tool["revision"] += 1
        else:
            raise AssertionError(f"unexpected statement: {sql}")


def _label(db, key):
    return next(label for label in list_labels(db) if label["key"] == key)


@pytest.mark.parametrize("second", ["Finanse", " finanse ", "FINANSE", "finanse\u00a0", "\uff26\uff29\uff2e\uff21\uff2e\uff33\uff25"])
def test_create_label_normalized_unique(second):
    db = FakeLabelDb()
    first = create_label(db, "Finanse")
    assert (first["key"], first["name"], first["protected"], first["usage"]) == ("finanse", "Finanse", False, 0)
    with pytest.raises(AccessError, match="already exists") as err:
        create_label(db, second)
    assert isinstance(err.value, CatalogConflict)
    assert len(db.labels) == 5


@pytest.mark.parametrize(("name", "key"), [
    ("Dane  osobowe", "dane-osobowe"), ("Zażółć gęślą", "zazolc-gesla"), ("日本", "label"),
    ("x" * 100, "x" * 56),
])
def test_create_label_key_is_ascii_slug(name, key):
    assert create_label(FakeLabelDb(), name)["key"] == key


def test_create_label_key_collision_gets_suffix():
    db = FakeLabelDb()
    assert create_label(db, "Finanse!")["key"] == "finanse"
    assert create_label(db, "Finanse?")["key"] == "finanse-2"
    assert create_label(db, "Finanse.")["key"] == "finanse-3"
    assert create_label(db, "Stub in test!")["key"] == "stub-in-test-2"


@pytest.mark.parametrize("name", ["", "   ", "x" * 101, None, 7])
def test_create_label_refuses_a_bad_name(name):
    with pytest.raises(AccessError, match="1 to 100 characters"):
        create_label(FakeLabelDb(), name)


def test_label_count_capped(monkeypatch):
    db = FakeLabelDb()
    monkeypatch.setattr(tool_catalog, "MAX_LABELS", 5)
    create_label(db, "one")
    with pytest.raises(AccessError, match="at most 5 labels"):
        create_label(db, "two")
    tool_id = db.add_tool("t")
    with pytest.raises(AccessError, match="at most 5 labels"):
        classify_tool(db, tool_id, revision=0, labels=[], new_label="two", review=None)
    assert MAX_LABELS == 200


@pytest.mark.parametrize("key", sorted(BUILTIN_LABEL_KEYS))
def test_protected_label_rename_and_delete_refused(key):
    db = FakeLabelDb()
    label_id = _label(db, key)["id"]
    with pytest.raises(AccessError, match="built-in label"):
        rename_label(db, label_id, "Other")
    with pytest.raises(AccessError, match="built-in label"):
        delete_label(db, label_id)
    assert db.labels[label_id]["label_key"] == key


def test_rename_keeps_key():
    db = FakeLabelDb()
    label = create_label(db, "Finanse")
    renamed = rename_label(db, label["id"], "Money matters")
    assert (renamed["key"], renamed["name"]) == ("finanse", "Money matters")
    create_label(db, "Other")
    with pytest.raises(CatalogConflict):
        rename_label(db, label["id"], " other ")
    with pytest.raises(AccessError, match="label not found"):
        rename_label(db, 999, "x")


def test_new_label_reused_on_second_tool():
    db = FakeLabelDb()
    a, b = db.add_tool("a"), db.add_tool("b")
    classify_tool(db, a, revision=0, labels=[], new_label="Finanse", review=None)
    out = classify_tool(db, b, revision=0, labels=[], new_label=" FINANSE ", review=None)
    assert out["labels"] == ["finanse"]
    assert len(db.labels) == 5
    assert _label(db, "finanse")["usage"] == 2


def test_delete_label_reports_usage_then_removes_assignments():
    db = FakeLabelDb()
    a, b = db.add_tool("a"), db.add_tool("b")
    db.add_tool("gone", present=0)
    for tool_id in (a, b, 3):
        classify_tool(db, tool_id, revision=0, labels=["stub-in-test"], new_label="Finanse", review=None)
    assert _label(db, "finanse")["usage"] == 2
    delete_label(db, _label(db, "finanse")["id"])
    assert [k for k, _m in db.overrides(a)] == ["stub-in-test"]
    assert "finanse" not in {label["key"] for label in list_labels(db)}
    with pytest.raises(AccessError, match="label not found"):
        delete_label(db, 999)


def test_list_labels_usage_counts_effective_sets():
    db = FakeLabelDb()
    db.add_tool("a", ["private-data"])
    b = db.add_tool("b", ["private-data"])
    db.add_tool("c")
    classify_tool(db, b, revision=0, labels=[], new_label=None, review=None)
    usage = {label["key"]: label["usage"] for label in list_labels(db)}
    assert usage == {"private-data": 1, "untrusted-content": 0, "external-communication": 0, "stub-in-test": 0}
    assert all(label["protected"] for label in list_labels(db))


def test_classify_writes_overrides_as_diff_against_defaults():
    db = FakeLabelDb()
    tool_id = db.add_tool("t", ["private-data", "stub-in-test"])
    out = classify_tool(db, tool_id, revision=0, labels=["private-data", "external-communication"],
                        new_label=None, review=None)
    assert db.overrides(tool_id) == {("external-communication", "add"), ("stub-in-test", "remove")}
    assert out == {"id": tool_id, "source": "module:demo", "labels": ["external-communication", "private-data"],
                   "reviewed": False, "revision": 1}
    classify_tool(db, tool_id, revision=1, labels=["private-data", "stub-in-test"], new_label=None, review=None)
    assert db.overrides(tool_id) == set()
    assert db.locked == ["tool_catalog", "tool_catalog"]


def test_classify_conflict_409():
    db = FakeLabelDb()
    tool_id = db.add_tool("t")
    classify_tool(db, tool_id, revision=0, labels=["private-data"], new_label=None, review=None)
    with pytest.raises(CatalogConflict, match="changed"):
        classify_tool(db, tool_id, revision=0, labels=[], new_label=None, review="reviewed")
    assert db.overrides(tool_id) == {("private-data", "add")}
    assert db.tools[tool_id]["revision"] == 1
    with pytest.raises(AccessError, match="tool not found"):
        classify_tool(db, 999, revision=0, labels=[], new_label=None, review=None)


def test_classify_creates_label_and_assigns_atomically():
    db = FakeLabelDb()
    tool_id = db.add_tool("t")
    db.fail_on = "UPDATE tool_catalog"
    with pytest.raises(RuntimeError):
        classify_tool(db, tool_id, revision=0, labels=["private-data"], new_label="Finanse", review=None)
    assert db.rollbacks == 1 and db.commits == 0
    assert len(db.labels) == 4 and db.assignments == {}
    db.fail_on = None
    with pytest.raises(AccessError, match="unknown label: nope"):
        classify_tool(db, tool_id, revision=0, labels=["nope"], new_label="Finanse", review=None)
    assert len(db.labels) == 4


def test_edit_labels_does_not_approve_review():
    db = FakeLabelDb()
    tool_id = db.add_tool("t")
    assert classify_tool(db, tool_id, revision=0, labels=["private-data"], new_label=None, review=None)["reviewed"] is False
    assert classify_tool(db, tool_id, revision=1, labels=[], new_label=None, review="reviewed")["reviewed"] is True
    assert classify_tool(db, tool_id, revision=2, labels=["untrusted-content"], new_label=None,
                         review=None)["reviewed"] is True
    assert db.tools[tool_id]["reviewed_hash"] == "c" * 40
    assert classify_tool(db, tool_id, revision=3, labels=[], new_label=None, review="to_review")["reviewed"] is False
    assert db.tools[tool_id]["reviewed_hash"] is None


@pytest.mark.parametrize("review", ["approved", ""])
def test_classify_refuses_unknown_review(review):
    db = FakeLabelDb()
    with pytest.raises(AccessError, match="review must be"):
        classify_tool(db, db.add_tool("t"), revision=0, labels=[], new_label=None, review=review)
