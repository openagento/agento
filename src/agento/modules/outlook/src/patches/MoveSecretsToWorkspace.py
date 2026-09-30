"""Move DEFAULT-scope Outlook Graph secrets to workspace scope.

Rows move as OPAQUE CIPHERTEXT — this patch never calls the decryptor, which is the whole
point of the toolbox-only boundary. When a workspace row already exists for the same path it
is the more specific operator-set value and wins: the DEFAULT row is deleted rather than
moved (``core_config_data`` has ``UNIQUE (scope, scope_id, path)``). When the destination
workspace is ambiguous, ``setup:upgrade`` stops with remediation rather than leaving a
readable DEFAULT-scope secret behind.
"""
from __future__ import annotations

import pymysql

_PATHS = (
    "outlook/outlook_client_secret",
    "outlook/outlook_cert_pem",
    "outlook/outlook_cert_password",
)


def _col(row, key: str, index: int):
    """Read a column from either a DictCursor row or a tuple row."""
    return row[key] if isinstance(row, dict) else row[index]


class MoveSecretsToWorkspace:
    def require(self) -> list[str]:
        return []

    def apply(self, conn: pymysql.Connection) -> int:
        with conn.cursor() as cur:
            # Static SQL with fixed placeholders — never an f-string. _PATHS has three
            # entries by construction; a generated IN-list would put string formatting
            # into a query, which this project does not do.
            cur.execute(
                "SELECT path FROM core_config_data "
                "WHERE scope = 'default' AND path IN (%s, %s, %s)",
                _PATHS,
            )
            stale = [_col(r, "path", 0) for r in cur.fetchall()]
            if not stale:
                return 0
            cur.execute("SELECT id, code FROM workspace ORDER BY id")
            workspaces = cur.fetchall()

        if len(workspaces) != 1:
            codes = ", ".join(_col(w, "code", 1) for w in workspaces) or "(none)"
            raise RuntimeError(
                "Outlook Graph secrets are stored at DEFAULT scope and cannot be migrated "
                f"automatically: {len(workspaces)} workspaces exist ({codes}). Re-set each "
                "secret at workspace scope, then delete the DEFAULT row:\n"
                "  agento config:set outlook/outlook_client_secret "
                "--scope workspace --scope-id <id>   # value read from stdin, never argv\n"
                "  agento config:remove outlook/outlook_client_secret --scope default"
            )

        ws_id = _col(workspaces[0], "id", 0)
        changed = 0
        with conn.cursor() as cur:
            for path in stale:
                cur.execute(
                    "SELECT 1 FROM core_config_data "
                    "WHERE scope = 'workspace' AND scope_id = %s AND path = %s",
                    (ws_id, path),
                )
                if cur.fetchone():
                    cur.execute(
                        "DELETE FROM core_config_data WHERE scope = 'default' AND path = %s",
                        (path,),
                    )
                else:
                    cur.execute(
                        "UPDATE core_config_data SET scope = 'workspace', scope_id = %s "
                        "WHERE path = %s AND scope = 'default'",
                        (ws_id, path),
                    )
                changed += cur.rowcount
        conn.commit()
        return changed
