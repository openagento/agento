"""Remove the switches and the role grants of the three tools miniapps no longer declares
(``miniapp``, ``miniapp_get_launch_spec``, ``miniapp_list``; D-MINIAPP-2). The approval in
``miniapp_activation`` replaced them."""
from __future__ import annotations

import pymysql


class RemoveMiniappTools:
    def require(self) -> list[str]:
        return []

    def apply(self, conn: pymysql.Connection) -> None:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM core_config_data WHERE path IN"
                        " ('tools/miniapp/is_enabled', 'tools/miniapp_get_launch_spec/is_enabled',"
                        " 'tools/miniapp_list/is_enabled')")
            cur.execute("DELETE FROM role_grant WHERE grant_kind = 'tool'"
                        " AND name IN ('miniapp', 'miniapp_get_launch_spec', 'miniapp_list')")
        conn.commit()
