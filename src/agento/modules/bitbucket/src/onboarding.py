from __future__ import annotations

import getpass
import logging

import pymysql

from agento.framework.scoped_config import Scope, scoped_config_set

from .toolbox_client import BitbucketToolboxClient

# All five must be present (effectively resolved) for a view's config to be usable.
_REQUIRED_PATHS = (
    "bitbucket/bitbucket_workspace",
    "bitbucket/bitbucket_email",
    "bitbucket/bitbucket_api_token",
    "bitbucket/bitbucket_account_uuid",
    "bitbucket/repo_allowlist",
)
# These MUST be set at a view's OWN agent_view scope (NEVER inherited from DEFAULT):
#  - bitbucket_api_token: the credential must never live at DEFAULT scope, because the framework's
#    bootstrap() resolves DEFAULT-scope obscure config and would DECRYPT it in the cron process. Keeping
#    it agent_view-scoped means bootstrap (DEFAULT-only) never sees it and only the toolbox decrypts it.
#  - account_uuid + repo_allowlist: run_lane's fan-out guard requires them at agent_view scope so a view
#    never inherits another's identity (broken attribution). (D-5 security / R4-1+D-11 fan-out)
# workspace + email are non-secret and may inherit DEFAULT/ENV.
_VIEW_SCOPED_PATHS = (
    "bitbucket/bitbucket_api_token",
    "bitbucket/bitbucket_account_uuid",
    "bitbucket/repo_allowlist",
)

_REQUIRED_SCOPES = (
    "read:user:bitbucket",
    "read:repository:bitbucket",
    "read:pullrequest:bitbucket",
    "write:pullrequest:bitbucket",
)


def _normalize_repo_entries(raw: str, workspace: str) -> tuple[str, str | None]:
    """Normalize the watched-repo input to BARE repo slugs (the form every caller uses).

    The allow-list is matched exactly against a tool's ``repo`` argument, and the URL is built as
    ``repositories/{workspace}/{repo}`` — so a ``workspace/repo`` entry can only ever produce a
    confusing miss ("repo X is not in the allow-list") or a 404. A ``workspace/`` prefix that matches
    the workspace just entered is stripped (same repository, written the other way); a DIFFERENT
    prefix is an error, because the workspace is fixed by config and cannot be widened here.

    Returns ``(normalized_csv, error)`` — exactly one of the two is meaningful.
    """
    names: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        name = entry
        if "/" in entry:
            prefix, _, rest = entry.partition("/")
            if prefix.strip().lower() != workspace.strip().lower() or not rest.strip() or "/" in rest:
                return "", (
                    f'repo "{entry}" is not a bare repo slug. Enter repository slugs without the '
                    f'workspace (e.g. "agento"); the workspace "{workspace}" is configured separately.'
                )
            name = rest.strip()
        if name not in seen:
            seen.add(name)
            names.append(name)
    if not names:
        return "", "at least one repo slug is required."
    return ",".join(names), None


def _evaluate_completeness(views: list[tuple[int, int]], rows: list[dict]) -> bool:
    """Pure completeness verdict, mirroring run_lane's usability rule (R5-1) so "complete" never means
    "inert".

    ``views`` = ``[(agent_view_id, workspace_id), ...]`` (active views). ``rows`` = raw
    ``core_config_data`` rows for ``bitbucket/*`` (``{scope, scope_id, path, value}``). A value is
    "present" iff non-empty (the encrypted token blob counts as present without decrypting).
    """
    by_scope: dict[tuple[str, int], dict[str, str]] = {}
    for r in rows:
        value = r.get("value")
        if value is None or value == "":
            continue
        key = (r["scope"], int(r["scope_id"]))
        by_scope.setdefault(key, {})[r["path"]] = value

    default_cfg = by_scope.get((Scope.DEFAULT, 0), {})

    def effective(view_id: int, ws_id: int, path: str) -> str | None:
        av = by_scope.get((Scope.AGENT_VIEW, view_id), {})
        if path in av:
            return av[path]
        ws = by_scope.get((Scope.WORKSPACE, ws_id), {})
        if path in ws:
            return ws[path]
        return default_cfg.get(path)

    def view_complete(view_id: int, ws_id: int) -> bool:
        return all(effective(view_id, ws_id, p) for p in _REQUIRED_PATHS)

    def view_own_scoped(view_id: int) -> bool:
        av = by_scope.get((Scope.AGENT_VIEW, view_id), {})
        return all(av.get(p) for p in _VIEW_SCOPED_PATHS)

    # Uniform rule (single- and multi-view): complete iff at least one active view has all required
    # fields resolved effectively AND owns the token + account_uuid + repo_allowlist at its own
    # agent_view scope. This both keeps the token off DEFAULT scope (security — bootstrap never decrypts
    # it) and guarantees correct per-view attribution (no DEFAULT fan-out). With zero active views there
    # is nothing to attribute config to, so it is never complete.
    return any(view_complete(vid, ws) and view_own_scoped(vid) for vid, ws in views)


class BitbucketOnboarding:
    def describe(self) -> str:
        return "Configure Bitbucket Cloud workspace, agent account, API token, and watched repos."

    def is_complete(self, conn: pymysql.connections.Connection) -> bool:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT av.id AS id, av.workspace_id AS workspace_id "
                "FROM agent_view av JOIN workspace w ON w.id = av.workspace_id "
                "WHERE av.is_active = 1 AND w.is_active = 1"
            )
            view_rows = cur.fetchall()
            cur.execute(
                "SELECT scope, scope_id, path, value FROM core_config_data WHERE path LIKE 'bitbucket/%%'"
            )
            cfg_rows = cur.fetchall()

        def _v(row, key, idx):
            return row[key] if isinstance(row, dict) else row[idx]

        views = [(int(_v(r, "id", 0)), int(_v(r, "workspace_id", 1))) for r in view_rows]
        rows = [
            {
                "scope": _v(r, "scope", 0),
                "scope_id": _v(r, "scope_id", 1),
                "path": _v(r, "path", 2),
                "value": _v(r, "value", 3),
            }
            for r in cfg_rows
        ]
        # Effective resolution is ENV -> DB -> config.json. ENV (CONFIG__BITBUCKET__*) is GLOBAL, so a
        # value set there satisfies a field for every view via fallback -> model it as a DEFAULT-scope
        # row. (It is global, not agent_view-scoped, so it correctly does NOT satisfy the multi-view
        # per-view-scope requirement.) config.json defaults for the required fields are empty/absent, so
        # they never make a field "present" and need not be layered.
        import os

        from agento.framework.config_resolver import path_to_env_key

        for path in _REQUIRED_PATHS:
            env_val = os.environ.get(path_to_env_key(path))
            if env_val:
                rows.append({"scope": Scope.DEFAULT, "scope_id": 0, "path": path, "value": env_val})
        return _evaluate_completeness(views, rows)

    def run(self, conn, config: dict, logger: logging.Logger) -> None:
        from agento.framework.bootstrap import get_module_config
        from agento.framework.cli import terminal
        from agento.framework.workspace import get_active_agent_views

        print("\n=== Bitbucket Cloud onboarding ===")
        core_cfg = get_module_config("core")
        toolbox_url = core_cfg.get("toolbox/url", "") if isinstance(core_cfg, dict) else ""
        if not toolbox_url:
            print(
                "  Error: core/toolbox/url not configured. The Bitbucket API token is verified inside the "
                "toolbox, so a reachable toolbox is required to onboard.\n"
                "  Set it (`agento config:set core/toolbox/url <url>`) and re-run, or configure manually "
                "with `agento config:set bitbucket/...` (see docs/modules/bitbucket.md)."
            )
            return

        # Bitbucket config is ALWAYS agent_view-scoped (the API token must never live at DEFAULT scope —
        # see _VIEW_SCOPED_PATHS), so an active agent_view is required to attribute it to.
        views = get_active_agent_views(conn)
        if not views:
            print(
                "  Error: no active agent_view. Bitbucket config is per-agent_view (the API token is never "
                "stored at default scope), so create/activate an agent_view first, then re-run."
            )
            return

        print(
            "  The API token needs these granular Atlassian API-token scopes (no scope implies another):\n"
            "    " + ", ".join(_REQUIRED_SCOPES)
        )

        client = BitbucketToolboxClient(toolbox_url)
        try:
            # Verify-before-save loop: nothing is written until a credential set verifies (D-4).
            while True:
                workspace = input("  Bitbucket workspace slug: ").strip()
                email = input("  Agent Atlassian account email: ").strip()
                api_token = getpass.getpass("  Atlassian API token: ").strip()
                repo_allowlist = input(
                    "  Watched repo slugs — bare slugs, no workspace/ prefix (comma-separated): "
                ).strip()

                if not (workspace and email and api_token and repo_allowlist):
                    print("  Error: workspace, email, API token and at least one repo are all required.")
                    if terminal.select("How to proceed?", ["Retry", "Abort (nothing saved)"]) == 1:
                        return
                    continue

                # Validate the repo slugs BEFORE the verify round-trip: a bad list is the operator's
                # typo, and there is no reason to send the credentials to Bitbucket to learn that.
                normalized, repo_error = _normalize_repo_entries(repo_allowlist, workspace)
                if repo_error:
                    print(f"  Error: {repo_error}")
                    if terminal.select("How to proceed?", ["Retry", "Abort (nothing saved)"]) == 1:
                        return
                    continue
                if normalized != repo_allowlist:
                    print(f"  Using repos: {normalized}")
                repo_allowlist = normalized

                try:
                    result = client.verify(workspace, email, api_token)
                except Exception as e:  # toolbox unreachable / network / non-200
                    print(f"  Error: could not verify via toolbox at {toolbox_url}: {e}")
                    if terminal.select("How to proceed?", ["Retry", "Abort (nothing saved)"]) == 1:
                        return
                    continue

                if result.get("ok"):
                    account_uuid = result.get("account_uuid") or ""
                    username = result.get("username") or ""
                    print(f"  Verified: authenticated as {username} ({account_uuid}).")
                    break

                detail = result.get("detail") or f"HTTP {result.get('status')}"
                print(f"  Error: credential verification failed ({detail}). Nothing saved.")
                if terminal.select("How to proceed?", ["Retry", "Abort (nothing saved)"]) == 1:
                    return
        finally:
            client.close()

        # Always write at an AGENT_VIEW scope (never DEFAULT): the token must not live at DEFAULT (else
        # bootstrap would decrypt it in cron), and agent_view-scoped account_uuid/repo_allowlist keep
        # per-view attribution correct. Auto-select the sole view; prompt when there are several.
        if len(views) > 1:
            idx = terminal.select(
                "Which agent_view owns this Bitbucket account?",
                [f"{av.code} ({av.label})" for av in views],
            )
            owner = views[idx]
        else:
            owner = views[0]
        scope, scope_id = Scope.AGENT_VIEW, owner.id
        scope_desc = f"agent_view '{owner.code}'"

        # Single transaction: commit only AFTER a successful verify.
        scoped_config_set(conn, "bitbucket/bitbucket_workspace", workspace, scope=scope, scope_id=scope_id)
        scoped_config_set(conn, "bitbucket/bitbucket_email", email, scope=scope, scope_id=scope_id)
        scoped_config_set(conn, "bitbucket/bitbucket_account_uuid", account_uuid, scope=scope, scope_id=scope_id)
        scoped_config_set(conn, "bitbucket/repo_allowlist", repo_allowlist, scope=scope, scope_id=scope_id)
        # Seed this agent_view's git commit identity so its commits link to this Bitbucket account.
        # Bitbucket links a commit by author email matching a verified account email — which is the
        # email entered here. The framework materializes ~/.gitconfig [user] from these at workspace:build.
        scoped_config_set(conn, "agent_view/identity/git_author_email", email, scope=scope, scope_id=scope_id)
        if username:
            scoped_config_set(conn, "agent_view/identity/git_author_name", username, scope=scope, scope_id=scope_id)
        scoped_config_set(
            conn, "bitbucket/bitbucket_api_token", api_token,
            scope=scope, scope_id=scope_id, encrypted=True,
        )
        conn.commit()
        logger.info("Bitbucket onboarding saved config at scope=%s", scope_desc)

        print(
            f"\n  Saved Bitbucket config at {scope_desc} scope.\n"
            "  Next steps (Bitbucket tools are opt-in; nothing acts until enabled):\n"
            "    1) Enable the capabilities you want (one tool per call), e.g.:\n"
            "       for t in bitbucket_get_pr bitbucket_get_pr_diff bitbucket_get_pr_comments \\\n"
            f"                bitbucket_add_comment; do agento tool:enable \"$t\" --agent-view {owner.code}; done\n"
            "    2) Confirm the watched repos: "
            f"agento config:get bitbucket/repo_allowlist\n"
            "    3) Turn the channel on (same scope as the config above):\n"
            f"       agento config:set bitbucket/enabled 1 --scope=agent_view --scope-id={owner.id}"
        )
