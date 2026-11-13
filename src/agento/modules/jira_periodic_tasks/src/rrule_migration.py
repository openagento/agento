"""cron -> RRULE for jira_periodic_tasks (AG-67 plan §6.2, §6.3).

One predicate for the upgrade check and the data patch: what the check passes, the patch can
convert. The check runs before migration 056, so every read here works on both table shapes
(``issue_key``/``agent_type`` before, ``source_ref``/``source`` after) and on a database that has
no ``schedule`` or ``core_config_data`` table yet (a fresh install).
"""
from __future__ import annotations

import json

from agento.framework.schedule import engine
from agento.framework.schedule.cron_convert import Unconvertible, cron_to_rrule

MAP_PATH = "jira_periodic_tasks/frequency_map"


def to_rrule(value: object) -> str:
    """A map value or a legacy cron expression as canonical RRULE text, or ``Unconvertible``."""
    if not isinstance(value, str):
        raise Unconvertible("not a string")
    try:
        return engine.normalize(value)
    except engine.InvalidSchedule:
        pass
    try:
        return engine.normalize(cron_to_rrule(value))
    except engine.InvalidSchedule as exc:
        raise Unconvertible(f"converts to no valid RRULE ({exc.code})") from None


def convert_map(text: str) -> tuple[dict[str, str], list[str]]:
    """A stored ``frequency_map`` with RRULE values, and one error per entry that has none."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}, ["is not JSON"]
    if not isinstance(data, dict):
        return {}, ["is not a JSON object"]
    converted, errors = {}, []
    for label, value in data.items():
        try:
            converted[label] = to_rrule(value)
        except Unconvertible as exc:
            errors.append(f"entry {label!r} ({value!r}): {exc}")
    return converted, errors


def _columns(cur, table: str) -> set[str]:
    cur.execute("SELECT COLUMN_NAME AS c FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))
    return {r["c"] for r in cur.fetchall()}


def map_rows(cur) -> list[dict]:
    """Every stored ``frequency_map`` override, in every scope: ``scope``, ``scope_id``, ``value``."""
    if not _columns(cur, "core_config_data"):
        return []
    cur.execute("SELECT scope, scope_id, value FROM core_config_data WHERE path = %s", (MAP_PATH,))
    return list(cur.fetchall())


def legacy_rows(cur) -> list[dict]:
    """Every schedule row that still holds a cron expression: ``id``, ``ref``, ``cron_expr``,
    ``agent_view_id``, ``enabled``, ``created_at``. The patch sets ``cron_expr`` to NULL."""
    cols = _columns(cur, "schedule")
    if "cron_expr" not in cols:
        return []
    ref = "source_ref" if "source_ref" in cols else "issue_key"
    view = "agent_view_id" if "agent_view_id" in cols else "NULL"
    cur.execute(f"SELECT id, {ref} AS ref, cron_expr, {view} AS agent_view_id, enabled, created_at "
                "FROM schedule WHERE cron_expr IS NOT NULL ORDER BY id")
    return list(cur.fetchall())


def must_convert(row: dict) -> bool:
    """A row that would fire after the upgrade. A disabled or viewless row that cannot be
    converted is disabled by the patch instead; it does not stop the upgrade."""
    return row["agent_view_id"] is not None and bool(row["enabled"])
