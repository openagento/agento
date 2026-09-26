"""Re-encrypt pre-scrypt values under the new key derivation.

Values used to be encrypted with a key that was a bare SHA-256 of
``AGENTO_ENCRYPTION_KEY`` — no salt, no work factor, so a stolen database was open
to a precomputed-table attack on the passphrase. Encryption now derives the key with
scrypt and a per-value salt (``aes256s:`` prefix).

``crypto.decrypt`` still reads the legacy prefix, so a deployment keeps working until
this patch runs; the patch is what actually removes the weak values. It only touches
rows that still carry the legacy prefix, so it is safe to re-run and cheap when there
are none.
"""
from agento.framework.crypto import decrypt, encrypt, is_legacy

_LEGACY = "aes256:%"


class RekeyToScrypt:
    def apply(self, conn):
        with conn.cursor() as cur:
            self._rekey(
                cur,
                "SELECT config_id, value FROM core_config_data "
                "WHERE encrypted = 1 AND value LIKE %s",
                "UPDATE core_config_data SET value = %s WHERE config_id = %s",
                "config_id",
                "value",
            )
            self._rekey(
                cur,
                "SELECT id, credentials FROM credential WHERE credentials LIKE %s",
                "UPDATE credential SET credentials = %s WHERE id = %s",
                "id",
                "credentials",
            )
        conn.commit()

    @staticmethod
    def _rekey(cur, select_sql, update_sql, id_column, column):
        cur.execute(select_sql, (_LEGACY,))
        rows = cur.fetchall()
        for row in rows:
            row_id, stored = (
                (row[id_column], row[column]) if isinstance(row, dict) else (row[0], row[1])
            )
            if not stored or not is_legacy(stored):
                continue
            cur.execute(update_sql, (encrypt(decrypt(stored)), row_id))

    def require(self):
        return []
