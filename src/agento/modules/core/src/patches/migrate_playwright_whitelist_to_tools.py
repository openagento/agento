BROWSER_TOOL_NAMES = [
    "browser_click",
    "browser_close",
    "browser_console_messages",
    "browser_drag",
    "browser_evaluate",
    "browser_file_upload",
    "browser_fill_form",
    "browser_handle_dialog",
    "browser_hover",
    "browser_install",
    "browser_navigate",
    "browser_navigate_back",
    "browser_network_requests",
    "browser_press_key",
    "browser_resize",
    "browser_run_code",
    "browser_select_option",
    "browser_snapshot",
    "browser_start_tracing",
    "browser_start_video",
    "browser_stop_tracing",
    "browser_stop_video",
    "browser_tabs",
    "browser_take_screenshot",
    "browser_type",
    "browser_wait_for",
]

_LEGACY_PATH = "core/playwright_tool_whitelist"


def _parse_list(value):
    """Split a legacy whitelist value the way ``browser.js``'s ``parseList`` did."""
    return [raw.strip().lower() for raw in (value or "").split(",") if raw.strip()]


class MigratePlaywrightWhitelistToTools:
    """Turn the retired ``core/playwright_tool_whitelist`` into per-tool enablement.

    Browser tools used to be selected by a comma-separated string that never appeared
    on the admin Tools screen. Each tool is now declared in ``core/module.json`` and
    gated on its own ``tools/<name>/is_enabled`` key.

    **Why every scope writes all known keys.** The whitelist was ONE value resolved through
    the scope chain, so an agent_view list *replaced* the default list. Per-tool keys
    resolve independently, so writing only the enabled names would let a narrower
    agent_view list *inherit* the default's extra tools — broadening access on upgrade.
    Each scope that had a legacy row therefore gets an explicit ``'1'``/``'0'`` for every
    name in ``BROWSER_TOOL_NAMES``, which reproduces replace semantics exactly.

    **Stale per-tool rows are cleared first, at every scope.** ``tool:enable`` validates only
    a name's shape, so ``tools/browser_*/is_enabled`` rows could already exist at scopes the
    whitelist ignored — dead under the old gate, but live once the whitelist is gone. Every one
    of those paths is therefore deleted at *all* scopes before the translated values are
    written, so only the legacy whitelist decides what ends up enabled.

    **A legacy ENV whitelist is deliberately NOT migrated.** Two reasons. It cannot be read
    reliably: this patch runs during ``setup:upgrade`` in the *cron* container, while
    ``CONFIG__CORE__PLAYWRIGHT_TOOL_WHITELIST`` is a *toolbox* container variable
    (``docker/.toolbox.env``), so its absence here proves nothing. And turning an ENV setting
    into permanent DB grants would silently widen durable state from a value the operator may
    simply unset. Its failure direction is safe — those tools resolve to disabled until enabled
    explicitly — which is what ``docker/README.md`` and the ``DECISIONS.md`` entry promise.
    """

    def apply(self, conn):
        known = set(BROWSER_TOOL_NAMES)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT scope, scope_id, value FROM core_config_data WHERE path = %s",
                (_LEGACY_PATH,),
            )
            rows = cur.fetchall()

            for name in sorted(known):
                cur.execute(
                    "DELETE FROM core_config_data WHERE path = %s",
                    (f"tools/{name}/is_enabled",),
                )

            for row in rows:
                if isinstance(row, dict):
                    scope, scope_id, value = row["scope"], row["scope_id"], row["value"]
                else:
                    scope, scope_id, value = row
                enabled = {n for n in _parse_list(value) if n in known}
                self._write_scope(cur, scope, scope_id, enabled, known)

            cur.execute("DELETE FROM core_config_data WHERE path = %s", (_LEGACY_PATH,))
        conn.commit()

    @staticmethod
    def _write_scope(cur, scope, scope_id, enabled, known):
        """Materialize an explicit value for every known browser tool at one scope."""
        for name in sorted(known):
            cur.execute(
                "INSERT INTO core_config_data "
                "(scope, scope_id, path, value, encrypted) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE value = VALUES(value)",
                (scope, scope_id, f"tools/{name}/is_enabled",
                 "1" if name in enabled else "0", 0),
            )

    def require(self):
        return []
