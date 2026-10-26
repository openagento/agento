"""get_usage_by_credential runs its grouped SQL on real MySQL (TST-3)."""
from __future__ import annotations

from agento.framework.agent_manager.models import UsageSummary
from agento.framework.agent_manager.usage_store import get_usage_by_credential

from .conftest import _test_connection


def test_usage_is_summed_per_listed_credential_inside_the_window():
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            ids = []
            for label in ("a", "b", "unlisted", "idle"):
                cur.execute("INSERT INTO credential (scope, agent_type, label, credentials) "
                            "VALUES ('claude', 'claude', %s, 'x')", (label,))
                ids.append(cur.lastrowid)
            a, b, unlisted, idle = ids
            cur.executemany(
                "INSERT INTO usage_log (credential_id, tokens_used, created_at) "
                "VALUES (%s, %s, NOW() - INTERVAL %s HOUR)",
                [(a, 300, 0), (a, 400, 1), (a, 9000, 13),  # the 13 h old row is outside 12 h
                 (b, 50, 2), (unlisted, 7, 0)],
            )
        usage = get_usage_by_credential(conn, [a, b, idle], window_hours=12)
    finally:
        conn.close()

    assert usage == {
        a: UsageSummary(credential_id=a, total_tokens=700, call_count=2),
        b: UsageSummary(credential_id=b, total_tokens=50, call_count=1),
    }
