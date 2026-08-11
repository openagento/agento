class MigrateToolboxUrlToCore:
    """Move legacy ``jira/toolbox_url`` rows to shared ``core/toolbox/url``.

    Prior to this change, the jira module owned ``toolbox_url`` as its own
    config field. It is now a cross-cutting setting owned by ``core`` so
    claude/codex WorkspaceAdapters can construct default MCP server URLs from it.
    """

    def apply(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT scope, scope_id, value, encrypted "
                "FROM core_config_data WHERE path = %s",
                ("jira/toolbox_url",),
            )
            rows = cur.fetchall()
            for row in rows:
                if isinstance(row, dict):
                    scope = row["scope"]
                    scope_id = row["scope_id"]
                    value = row["value"]
                    encrypted = row["encrypted"]
                else:
                    scope, scope_id, value, encrypted = row
                cur.execute(
                    "INSERT IGNORE INTO core_config_data "
                    "(scope, scope_id, path, value, encrypted) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (scope, scope_id, "core/toolbox/url", value, encrypted),
                )
            cur.execute(
                "DELETE FROM core_config_data WHERE path = %s",
                ("jira/toolbox_url",),
            )
        conn.commit()

    def require(self):
        return []
