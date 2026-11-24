"""Tool catalog: one row per (source, tool name), synced from the enabled module manifests.

Effective labels = the manifest's default labels, plus the admin's ``add`` rows, minus the
admin's ``remove`` rows. A tool is reviewed while ``reviewed_hash`` equals ``contract_hash``.
The sync never touches an assignment, so labels survive a tool's absence and rediscovery.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pymysql

from .access.accounts import AccessError
from .config_resolver import path_to_env_key, read_config_defaults
from .config_schema import env_allowed, is_toolbox_only
from .module_loader import ModuleManifest

RISK_KEYS = ("private-data", "untrusted-content", "external-communication")
STUB_IN_TEST = "stub-in-test"
BUILTIN_LABEL_KEYS = frozenset((*RISK_KEYS, STUB_IN_TEST))
SOURCE_PREFIX = "module:"
CONTRACT_LOCK_FILE = "tool-contracts.json"
MAX_LABELS = 200
LABEL_NAME_MAX = 100
_KEY_STEM_MAX = 56
_LABEL_KEY = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_NOT_DECOMPOSED = str.maketrans({"ł": "l", "đ": "d", "ø": "o", "æ": "ae", "œ": "oe"})
_REVIEW_SQL = {"reviewed": "reviewed_hash = contract_hash, ", "to_review": "reviewed_hash = NULL, ", None: ""}


class NoEnabledModules(RuntimeError):
    """The scan found no enabled module: a failed discovery must not mark every tool absent (SEC-9)."""


@dataclass(frozen=True)
class CatalogEntry:
    source: str
    tool_name: str
    alias: str
    toolset: str | None
    description: str | None
    default_labels: frozenset[str]
    contract_hash: str


@dataclass(frozen=True)
class CatalogTool:
    id: int
    source: str
    tool_name: str
    alias: str
    toolset: str | None
    description: str | None
    requires: str | None
    labels: frozenset[str]
    reviewed: bool
    contract_hash: str
    revision: int
    present: bool


@dataclass(frozen=True)
class SyncResult:
    inserted: int
    changed: int
    absent: int


def _sha1(value: object) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def is_in_tree(manifest: ModuleManifest) -> bool:
    from .module_validator import is_core_module

    return is_core_module(manifest.path)


def default_labels(tool: Mapping, config_defaults: Mapping, *, in_tree: bool) -> frozenset[str]:
    """The labels a manifest tool entry brings before any admin override.

    The merged ``tools/<name>/sensitive == "1"`` default reads as ``stub-in-test`` (PRD §7).
    An out-of-tree module contributes only ``stub-in-test``; its risk labels come from the admin.
    """
    declared = tool.get("labels")
    labels = {label for label in declared if isinstance(label, str)} if isinstance(declared, list) else set()
    if str(config_defaults.get(f"tools/{tool.get('name')}/sensitive", "")) == "1":
        labels.add(STUB_IN_TEST)
    return frozenset(labels if in_tree else labels & {STUB_IN_TEST})


def _fingerprinted_fields(system: Mapping) -> list[tuple[str, Mapping]]:
    return [
        (name, field) for name, field in system.items()
        if isinstance(field, dict) and field.get("review_fingerprint") is True
        and field.get("type") != "obscure" and not is_toolbox_only(field)
    ]


def module_fingerprint(targets: Mapping[str, Mapping[str, Any]]) -> str:
    """Hash of the effective target of each flagged path; only hashes of values enter.

    A set ``CONFIG__*`` value wins over every scope, so it alone counts and a masked DB edit
    changes nothing. Otherwise every ``core_config_data`` row of the path and the config.json
    default count.
    """
    def per_path(target: Mapping[str, Any]) -> list:
        if target.get("env") is not None:
            return [["env", _sha1(target["env"])]]
        rows = sorted([scope, int(scope_id), _sha1(value)] for scope, scope_id, value in target.get("rows", ()))
        return [*rows, ["file", _sha1(target.get("file"))]]

    return _sha1({path: per_path(target) for path, target in targets.items()}) if targets else ""


def review_fingerprints(conn, manifests: Iterable[ModuleManifest], *,
                        environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """``{module: fingerprint}`` over the ``review_fingerprint`` fields of each module's system.json."""
    flagged = [(m, _fingerprinted_fields(m.config)) for m in manifests]
    paths = [f"{m.name}/{name}" for m, fields in flagged for name, _ in fields]
    if not paths:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT path, scope, scope_id, value FROM core_config_data "
            f"WHERE path IN ({', '.join(['%s'] * len(paths))})",
            tuple(paths),
        )
        rows = cur.fetchall()
    rows_by_path: dict[str, list[tuple]] = {}
    for row in rows:
        rows_by_path.setdefault(row["path"], []).append((row["scope"], row["scope_id"], row["value"]))
    fingerprints = {}
    for m, fields in flagged:
        if not fields:
            continue
        file_defaults = read_config_defaults(m.path)
        targets = {}
        for name, field in fields:
            path = f"{m.name}/{name}"
            env_value = environ.get(path_to_env_key(path)) if env_allowed(field) else None
            targets[path] = {"env": env_value, "rows": rows_by_path.get(path, []), "file": file_defaults.get(name)}
        fingerprints[m.name] = module_fingerprint(targets)
    return fingerprints


def _read_lock(module_dir: Path) -> dict:
    try:
        lock = json.loads((module_dir / CONTRACT_LOCK_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return lock if isinstance(lock, dict) else {}


def manifest_entries(
    manifests: Iterable[ModuleManifest],
    config_defaults: Mapping,
    *,
    in_tree: Callable[[ModuleManifest], bool] = is_in_tree,
    fingerprints: Mapping[str, str] | None = None,
) -> list[CatalogEntry]:
    """One catalog entry per declared tool. ``config_defaults`` is the merged config.json."""
    entries = []
    for m in manifests:
        lock = _read_lock(m.path)
        env = (fingerprints or {}).get(m.name, "")
        module_in_tree = in_tree(m)
        for tool in m.tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            name = tool["name"]
            labels = default_labels(tool, config_defaults, in_tree=module_in_tree)
            source = f"{SOURCE_PREFIX}{m.name}"
            contract = _sha1({
                "source": source, "tool_name": name, "description": tool.get("description"),
                "toolset": tool.get("toolset"), "requires": tool.get("requires"),
                "default_labels": sorted(labels), "schema": lock.get(name, m.version), "env": env,
            })
            entries.append(CatalogEntry(
                source=source, tool_name=name, alias=name, toolset=tool.get("toolset"),
                description=tool.get("description"), default_labels=labels, contract_hash=contract,
            ))
    return entries


def _labels_of(raw: object) -> frozenset[str]:
    parsed = json.loads(raw) if isinstance(raw, str | bytes) else raw
    return frozenset(parsed or ())


def sync_catalog(conn, entries: Iterable[CatalogEntry]) -> SyncResult:
    """Make the ``module:*`` rows match ``entries``, in one transaction.

    Writes only what differs; a changed contract bumps ``revision``, so an editor opened on
    the old contract gets a conflict. A row not in ``entries`` is marked absent, never deleted.
    """
    wanted = {(e.source, e.tool_name): e for e in entries}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, source, tool_name, alias, toolset, description, default_labels, "
                "contract_hash, present FROM tool_catalog WHERE source LIKE %s",
                (f"{SOURCE_PREFIX}%",),
            )
            existing = {(r["source"], r["tool_name"]): r for r in cur.fetchall()}
            inserts, updates = [], []
            for key, e in wanted.items():
                labels = json.dumps(sorted(e.default_labels))
                row = existing.get(key)
                if row is None:
                    inserts.append((e.source, e.tool_name, e.alias, e.toolset, e.description, labels, e.contract_hash))
                elif (row["alias"], row["toolset"], row["description"], _labels_of(row["default_labels"]),
                      row["contract_hash"], bool(row["present"])) != (
                        e.alias, e.toolset, e.description, e.default_labels, e.contract_hash, True):
                    bump = int(row["contract_hash"] != e.contract_hash)
                    updates.append((e.alias, e.toolset, e.description, labels, bump, e.contract_hash, row["id"]))
            gone = sorted(r["id"] for key, r in existing.items() if key not in wanted and r["present"])
            if inserts:
                cur.executemany(
                    "INSERT INTO tool_catalog (source, tool_name, alias, toolset, description, "
                    "default_labels, contract_hash) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE id = id",
                    inserts,
                )
            if updates:
                cur.executemany(
                    "UPDATE tool_catalog SET alias = %s, toolset = %s, description = %s, "
                    "default_labels = %s, revision = revision + %s, contract_hash = %s, present = 1 "
                    "WHERE id = %s",
                    updates,
                )
            if gone:
                cur.execute(
                    f"UPDATE tool_catalog SET present = 0 WHERE id IN ({', '.join(['%s'] * len(gone))})",
                    tuple(gone),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return SyncResult(inserted=len(inserts), changed=len(updates), absent=len(gone))


def load_catalog(conn, *, manifests: Iterable[ModuleManifest] | None = None) -> list[CatalogTool]:
    """Every present catalog tool with its effective labels, in one query (the admin screen)."""
    if manifests is None:
        from .bootstrap import get_manifests

        manifests = get_manifests()
    requires = {
        (f"{SOURCE_PREFIX}{m.name}", t["name"]): t.get("requires")
        for m in manifests for t in m.tools if isinstance(t, dict) and isinstance(t.get("name"), str)
    }
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.id, c.source, c.tool_name, c.alias, c.toolset, c.description, c.default_labels, "
            "c.contract_hash, c.reviewed_hash, c.revision, c.present, l.label_key, a.mode "
            "FROM tool_catalog c "
            "LEFT JOIN tool_label_assignment a ON a.tool_id = c.id "
            "LEFT JOIN tool_label l ON l.id = a.label_id "
            "WHERE c.present = 1 ORDER BY c.id",
        )
        rows = cur.fetchall()
    by_id: dict[int, dict] = {}
    for r in rows:
        tool = by_id.setdefault(r["id"], {"row": r, "add": set(), "remove": set()})
        if r["mode"] in ("add", "remove"):
            tool[r["mode"]].add(r["label_key"])
    tools = []
    for tool in by_id.values():
        r = tool["row"]
        tools.append(CatalogTool(
            id=r["id"], source=r["source"], tool_name=r["tool_name"], alias=r["alias"],
            toolset=r["toolset"], description=r["description"],
            requires=requires.get((r["source"], r["tool_name"])),
            labels=(_labels_of(r["default_labels"]) | tool["add"]) - tool["remove"],
            reviewed=r["reviewed_hash"] == r["contract_hash"], contract_hash=r["contract_hash"],
            revision=r["revision"], present=bool(r["present"]),
        ))
    return tools


def sync_enabled_modules(conn, *, core_dir: str | None = None, user_dir: str | None = None) -> SyncResult:
    """Sync the catalog from the enabled modules on disk. An empty module set never syncs (SEC-9)."""
    from .bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
    from .module_discovery import scan_all_modules
    from .module_status import filter_enabled

    enabled = filter_enabled(scan_all_modules(core_dir or CORE_MODULES_DIR, user_dir or USER_MODULES_DIR))
    if not enabled:
        raise NoEnabledModules("no enabled module found; the tool catalog was not synced")
    config_defaults: dict = {}
    for m in enabled:
        config_defaults.update(read_config_defaults(m.path))
    return sync_catalog(conn, manifest_entries(enabled, config_defaults,
                                               fingerprints=review_fingerprints(conn, enabled)))


class CatalogConflict(AccessError):
    """The write lost to another one: a label name already taken, or a tool changed since it was read."""


def _in_transaction(conn, work):
    conn.begin()
    try:
        with conn.cursor() as cur:
            result = work(cur)
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise


def _label_names(name: object) -> tuple[str, str]:
    """``(display name, name_norm)``: NFKC, casefold, trimmed, inner whitespace collapsed."""
    if not isinstance(name, str):
        raise AccessError(f"label name must have 1 to {LABEL_NAME_MAX} characters")
    display = " ".join(name.split())
    norm = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
    if not display or len(display) > LABEL_NAME_MAX or len(norm) > LABEL_NAME_MAX:
        raise AccessError(f"label name must have 1 to {LABEL_NAME_MAX} characters")
    return display, norm


def _key_stem(norm: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", norm.translate(_NOT_DECOMPOSED)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text)[:_KEY_STEM_MAX].strip("-") or "label"


def _label_json(row: Mapping, used: int = 0) -> dict:
    return {"id": row["id"], "key": row["label_key"], "name": row["name"],
            "protected": bool(row["is_protected"]), "usage": int(used)}


def _insert_label(cur, name: object, *, reuse: bool) -> dict:
    """A new custom label; with ``reuse`` a label of the same normalized name is returned instead."""
    display, norm = _label_names(name)
    cur.execute("SELECT id, label_key, name, is_protected FROM tool_label WHERE name_norm = %s", (norm,))
    existing = cur.fetchone()
    if existing:
        if reuse:
            return _label_json(existing)
        raise CatalogConflict("a label with this name already exists")
    cur.execute("SELECT COUNT(*) AS n FROM tool_label")
    if cur.fetchone()["n"] >= MAX_LABELS:
        raise AccessError(f"there can be at most {MAX_LABELS} labels")
    stem = _key_stem(norm)
    cur.execute("SELECT label_key FROM tool_label WHERE label_key = %s OR label_key LIKE %s", (stem, f"{stem}-%"))
    taken = {r["label_key"] for r in cur.fetchall()}
    key = next(k for n in range(1, len(taken) + 2) if (k := stem if n == 1 else f"{stem}-{n}") not in taken)
    try:
        cur.execute("INSERT INTO tool_label (label_key, name, name_norm) VALUES (%s, %s, %s)", (key, display, norm))
    except pymysql.err.IntegrityError:
        raise CatalogConflict("a label with this name already exists") from None
    return {"id": cur.lastrowid, "key": key, "name": display, "protected": False, "usage": 0}


def list_labels(conn) -> list[dict]:
    """``[{id, key, name, protected, usage}]``; ``usage`` counts the present tools whose effective labels hold it.

    Counted here, not in SQL: a label is in a tool's set by default or by assignment, and a
    join of every label with every tool grows as labels times tools (SCL-1).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT l.id, l.label_key, l.name, l.is_protected FROM tool_label l"
                    " ORDER BY l.is_protected DESC, l.name")
        labels = cur.fetchall()
        cur.execute("SELECT c.id, c.default_labels, a.label_id, a.mode FROM tool_catalog c"
                    " LEFT JOIN tool_label_assignment a ON a.tool_id = c.id WHERE c.present = 1")
        rows = cur.fetchall()
    key_of = {r["id"]: r["label_key"] for r in labels}
    tools: dict[int, set[str]] = {}
    modes: dict[int, dict[str, str]] = {}
    for r in rows:
        tools.setdefault(r["id"], set(_labels_of(r["default_labels"])))
        if r["label_id"] in key_of:
            modes.setdefault(r["id"], {})[key_of[r["label_id"]]] = r["mode"]
    used: dict[str, int] = {}
    for tool_id, defaults in tools.items():
        mine = modes.get(tool_id, {})
        for key in (defaults - {k for k, m in mine.items() if m == "remove"}) | {k for k, m in mine.items() if m == "add"}:
            used[key] = used.get(key, 0) + 1
    return [_label_json(r, used.get(r["label_key"], 0)) for r in labels]


def create_label(conn, name: str) -> dict:
    return _in_transaction(conn, lambda cur: _insert_label(cur, name, reuse=False))


def _lock_custom_label(cur, label_id: int) -> None:
    cur.execute("SELECT id, is_protected FROM tool_label WHERE id = %s FOR UPDATE", (label_id,))
    row = cur.fetchone()
    if not row:
        raise AccessError("label not found")
    if row["is_protected"]:
        raise AccessError("a built-in label cannot be renamed or deleted")


def rename_label(conn, label_id: int, name: str) -> dict:
    """The name only: the key never changes, because a CLI flag or a stored policy names it."""
    display, norm = _label_names(name)

    def work(cur):
        _lock_custom_label(cur, label_id)
        cur.execute("SELECT id FROM tool_label WHERE name_norm = %s AND id <> %s", (norm, label_id))
        if cur.fetchone():
            raise CatalogConflict("a label with this name already exists")
        try:
            cur.execute("UPDATE tool_label SET name = %s, name_norm = %s WHERE id = %s", (display, norm, label_id))
        except pymysql.err.IntegrityError:
            raise CatalogConflict("a label with this name already exists") from None

    _in_transaction(conn, work)
    return next(label for label in list_labels(conn) if label["id"] == label_id)


def delete_label(conn, label_id: int) -> None:
    """Its assignments go with it in the same transaction (FK cascade)."""
    def work(cur):
        _lock_custom_label(cur, label_id)
        cur.execute("DELETE FROM tool_label WHERE id = %s", (label_id,))

    _in_transaction(conn, work)


def classify_tool(conn, tool_id: int, *, revision: int, labels: Iterable[str], new_label: str | None,
                  review: Literal["reviewed", "to_review"] | None) -> dict:
    """Set a tool's effective labels and, optionally, its review status, in one transaction.

    ``labels`` are keys; ``new_label`` is a name, created (or reused by its normalized name) and
    assigned. Stored overrides are the diff against the manifest defaults. A ``revision`` other
    than the row's is a conflict. Returns ``{id, source, labels, reviewed, revision}``.
    """
    if review not in _REVIEW_SQL:
        raise AccessError("review must be reviewed, to_review or null")
    wanted = set(labels)
    if len(wanted) > MAX_LABELS:
        raise AccessError(f"there can be at most {MAX_LABELS} labels")

    def work(cur):
        cur.execute(
            "SELECT id, source, default_labels, contract_hash, reviewed_hash, revision FROM tool_catalog"
            " WHERE id = %s FOR UPDATE", (tool_id,),
        )
        tool = cur.fetchone()
        if not tool:
            raise AccessError("tool not found")
        if tool["revision"] != revision:
            raise CatalogConflict("the tool was changed since it was read; reload it")
        if new_label is not None:
            wanted.add(_insert_label(cur, new_label, reuse=True)["key"])
        defaults = _labels_of(tool["default_labels"])
        changed = {key: "add" for key in wanted - defaults} | {key: "remove" for key in defaults - wanted}
        ids = {}
        if changed:
            unknown = sorted(k for k in changed if not _LABEL_KEY.fullmatch(k) or len(k) > 64)
            if not unknown:
                keys = sorted(changed)
                cur.execute(f"SELECT id, label_key FROM tool_label WHERE label_key IN ({', '.join(['%s'] * len(keys))})",
                            tuple(keys))
                ids = {r["label_key"]: r["id"] for r in cur.fetchall()}
                unknown = sorted(set(changed) - ids.keys())
            if unknown:
                raise AccessError(f"unknown label: {', '.join(unknown)}")
        cur.execute("DELETE FROM tool_label_assignment WHERE tool_id = %s", (tool_id,))
        if changed:
            cur.executemany("INSERT INTO tool_label_assignment (tool_id, label_id, mode) VALUES (%s, %s, %s)",
                            [(tool_id, ids[key], mode) for key, mode in sorted(changed.items())])
        cur.execute(f"UPDATE tool_catalog SET {_REVIEW_SQL[review]}revision = revision + 1 WHERE id = %s", (tool_id,))
        reviewed = {"reviewed": True, "to_review": False}.get(review, tool["reviewed_hash"] == tool["contract_hash"])
        return {"id": tool["id"], "source": tool["source"], "labels": sorted(wanted), "reviewed": reviewed,
                "revision": tool["revision"] + 1}

    return _in_transaction(conn, work)
