"""The miniapps tools left the module (D-MINIAPP-2): their config rows and role grants go too (CLS-1)."""
from __future__ import annotations

from agento.modules.miniapps.src.patches.RemoveMiniappTools import RemoveMiniappTools

from .conftest import _test_connection

GONE = ("miniapp", "miniapp_get_launch_spec", "miniapp_list")


def test_the_patch_removes_the_old_tool_switches_and_grants_only():
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM role_grant WHERE role = 'user' AND grant_kind = 'tool'")
            for scope, scope_id in (("default", 0), ("agent_view", 7)):
                for tool in (*GONE, "miniapp_keep"):
                    cur.execute("REPLACE INTO core_config_data (scope, scope_id, path, value) VALUES (%s, %s, %s, '1')",
                                (scope, scope_id, f"tools/{tool}/is_enabled"))
            for tool in (*GONE, "miniapp_keep"):
                cur.execute("INSERT INTO role_grant (role, grant_kind, name) VALUES ('user', 'tool', %s)", (tool,))
        RemoveMiniappTools().apply(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT path FROM core_config_data WHERE path LIKE 'tools/miniapp%%'")
            assert {r["path"] for r in cur.fetchall()} == {"tools/miniapp_keep/is_enabled"}
            cur.execute("SELECT name FROM role_grant WHERE role = 'user' AND grant_kind = 'tool'")
            assert [r["name"] for r in cur.fetchall()] == ["miniapp_keep"]
        assert RemoveMiniappTools().require() == []
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM core_config_data WHERE path LIKE 'tools/miniapp%%'")
            cur.execute("DELETE FROM role_grant WHERE role = 'user' AND grant_kind = 'tool'")
        conn.close()
