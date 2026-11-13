"""setup:upgrade pre-flight (AG-67 plan §6.3): refuse the upgrade while a cron value has no RRULE twin.

Read only. It runs before any migration, so a refusal leaves the database as it was.
After ConvertFrequencyMapToRrule ran, nothing is pending: a later bad value is the sync's to skip.
"""
from __future__ import annotations

from agento.framework.data_patch import get_applied_patches
from agento.framework.schedule.cron_convert import Unconvertible

from .rrule_migration import MAP_PATH, convert_map, legacy_rows, map_rows, must_convert, to_rrule


class FrequencyMapCheck:
    def check(self, conn) -> list[str]:
        if "ConvertFrequencyMapToRrule" in get_applied_patches(conn, "jira_periodic_tasks"):
            return []
        with conn.cursor() as cur:
            maps, rows = map_rows(cur), legacy_rows(cur)
        messages = []
        for m in maps:
            _, errors = convert_map(m["value"])
            messages += [f"{MAP_PATH} ({m['scope']} {m['scope_id']}) {e}; set it to an RRULE "
                         "(agento config:set) and upgrade again" for e in errors]
        for r in rows:
            if not must_convert(r):
                continue
            try:
                to_rrule(r["cron_expr"])
            except Unconvertible as exc:
                messages.append(f"schedule row {r['id']} ({r['ref']}) cron {r['cron_expr']!r} {exc}; "
                                f"disable it (UPDATE schedule SET enabled = 0 WHERE id = {r['id']}) "
                                "and upgrade again")
        return messages
