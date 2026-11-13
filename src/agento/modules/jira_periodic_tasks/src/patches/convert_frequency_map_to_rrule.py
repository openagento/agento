"""Data patch (AG-67 plan §6.2): the stored ``frequency_map`` and the legacy schedule rows become RRULE.

Runs after migration 056. A legacy row becomes a ``jira_periodic`` row owned by its view, with
``next_fire_at`` NULL so the tick computes its first fire. The upgrade check has refused every
value this patch cannot convert, so a failure here is a defect, not an operator error.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time

from agento.framework.schedule.cron_convert import Unconvertible
from agento.framework.schedule.tick import default_timezone

from ..rrule_migration import MAP_PATH, convert_map, legacy_rows, map_rows, must_convert, to_rrule

logger = logging.getLogger(__name__)


class ConvertFrequencyMapToRrule:
    def apply(self, conn) -> None:
        tz = default_timezone(conn)
        with conn.cursor() as cur:
            for m in map_rows(cur):
                converted, errors = convert_map(m["value"])
                if errors:
                    raise RuntimeError(f"{MAP_PATH} ({m['scope']} {m['scope_id']}): {'; '.join(errors)}")
                cur.execute("UPDATE core_config_data SET value = %s WHERE scope = %s AND scope_id = %s "
                            "AND path = %s", (json.dumps(converted), m["scope"], m["scope_id"], MAP_PATH))
            for r in legacy_rows(cur):
                try:
                    rrule = to_rrule(r["cron_expr"])
                except Unconvertible:
                    if must_convert(r):
                        raise
                    rrule = None
                error = None
                if r["agent_view_id"] is None:
                    error = "no_agent_view"  # no target view: never fires (SEC-9)
                    logger.warning("schedule row %s (%s) has no agent_view; disabled", r["id"], r["ref"])
                elif rrule is None:
                    error = "bad_rule"
                cur.execute(
                    "UPDATE schedule SET source = 'jira_periodic', owner_agent_view_id = %s, rrule = %s, "
                    "starts_at = %s, timezone = %s, next_fire_at = NULL, anchor_local = NULL, "
                    "cron_expr = NULL, enabled = IF(%s IS NULL, enabled, 0), last_error = %s WHERE id = %s",
                    (r["agent_view_id"], rrule, datetime.combine(r["created_at"].date(), time()), tz,
                     error, error, r["id"]),
                )
        conn.commit()

    def require(self) -> list[str]:
        return []
