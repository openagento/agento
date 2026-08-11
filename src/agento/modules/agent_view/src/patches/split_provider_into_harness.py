"""Split the pre-0.15 single-axis ``agent_view/provider`` into harness + provider.

Before the split, ``agent_view/provider`` held what is now called the HARNESS
("claude"/"codex"). This copies that value to ``agent_view/harness`` and rewrites
``agent_view/provider`` to the corresponding model vendor.

The mapping is FROZEN here on purpose, unlike the generic runtime fallback in
``agent_view_runtime``. A data patch is applied once and tracked permanently
(``data_patch`` table), so it must not depend on which modules happen to be enabled
at upgrade time: reading the harness registry would skip ``provider=codex`` rows on a
deployment with the ``codex`` module disabled, and a later ``module:enable codex``
would never re-run the patch — leaving that config permanently unmigrated.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Historical values only. New harnesses do not belong here — they never had a
# pre-0.15 config to migrate.
_HISTORICAL_HARNESS_PROVIDERS = {
    "claude": "anthropic",
    "codex": "openai",
}


class SplitProviderIntoHarness:
    def apply(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT scope, scope_id, value FROM core_config_data "
                "WHERE path = 'agent_view/provider'"
            )
            rows = cur.fetchall()

            for row in rows:
                scope = row["scope"] if isinstance(row, dict) else row[0]
                scope_id = row["scope_id"] if isinstance(row, dict) else row[1]
                value = row["value"] if isinstance(row, dict) else row[2]

                provider = _HISTORICAL_HARNESS_PROVIDERS.get(value)
                if provider is None:
                    # Already migrated, or a value this patch knows nothing about.
                    # Leave it alone and say so rather than guessing.
                    logger.info(
                        "agent_view/provider=%r at (%s,%s) is not a pre-0.15 harness "
                        "value; leaving it untouched.", value, scope, scope_id,
                    )
                    continue

                # Never overwrite a harness someone already set at this scope (e.g.
                # configured by hand after the upgrade, before the patch ran).
                cur.execute(
                    "INSERT IGNORE INTO core_config_data (scope, scope_id, path, value) "
                    "VALUES (%s, %s, 'agent_view/harness', %s)",
                    (scope, scope_id, value),
                )
                cur.execute(
                    "UPDATE core_config_data SET value = %s "
                    "WHERE path = 'agent_view/provider' AND scope = %s AND scope_id = %s",
                    (provider, scope, scope_id),
                )
                logger.info(
                    "Migrated (%s,%s): harness=%r provider=%r", scope, scope_id, value, provider
                )
        conn.commit()

    def require(self):
        return ["RenameAgentConfigPrefix"]
