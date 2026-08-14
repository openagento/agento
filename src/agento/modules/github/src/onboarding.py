from __future__ import annotations

import getpass
import logging

import pymysql

from agento.framework.scoped_config import Scope, scoped_config_set

from .env_guard import offending_env_keys
from .toolbox_client import GitHubToolboxClient

# All four must be present (effectively resolved) for a view's config to be usable. There is no email
# field: GitHub identifies the agent by its login, and the git author email is derived from it.
_REQUIRED_PATHS = (
    "github/github_owner",
    "github/github_login",
    "github/github_token",
    "github/repo_allowlist",
)
# These MUST be set at a view's OWN agent_view scope (NEVER inherited from DEFAULT):
#  - github_token: the credential must never live at DEFAULT scope, because the framework's bootstrap()
#    resolves DEFAULT-scope obscure config and would DECRYPT it in the cron process. Keeping it
#    agent_view-scoped means bootstrap (DEFAULT-only) never sees it and only the toolbox decrypts it.
#  - github_login + repo_allowlist: run_lane's fan-out guard requires them at agent_view scope so a view
#    never inherits another's identity (broken attribution).
# github_owner is non-secret and may inherit DEFAULT/ENV.
_VIEW_SCOPED_PATHS = (
    "github/github_token",
    "github/github_login",
    "github/repo_allowlist",
)

_REQUIRED_PERMISSIONS = (
    "Pull requests: Read and write",
    "Contents: Read-only (needed to read the PR head commit)",
    "Metadata: Read-only (mandatory)",
)


def _evaluate_completeness(views: list[tuple[int, int]], rows: list[dict]) -> bool:
    """Pure completeness verdict, mirroring run_lane's usability rule so "complete" never means "inert".

    ``views`` = ``[(agent_view_id, workspace_id), ...]`` (active views). ``rows`` = raw
    ``core_config_data`` rows for ``github/*`` (``{scope, scope_id, path, value}``). A value is
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
    # fields resolved effectively AND owns the token + login + repo_allowlist at its own agent_view
    # scope. With zero active views there is nothing to attribute config to, so it is never complete.
    return any(view_complete(vid, ws) and view_own_scoped(vid) for vid, ws in views)


class GitHubOnboarding:
    def describe(self) -> str:
        return "Configure the GitHub owner, agent account PAT, and watched repos."

    def is_complete(self, conn: pymysql.connections.Connection) -> bool:
        # The ENV half of the fail-closed rule (the other half is run_lane). A global
        # CONFIG__GITHUB__* override wins in resolve_field for EVERY view, so however correct the DB
        # rows are, this deployment resolves one shared identity — which is not a complete setup.
        offenders = offending_env_keys()
        if offenders:
            print(
                f"  github: {', '.join(offenders)} are set as global ENV overrides; these fields must be "
                "configured per agent_view (`config:set … --scope=agent_view`). Unset them and re-run."
            )
            return False

        with conn.cursor() as cur:
            cur.execute(
                "SELECT av.id AS id, av.workspace_id AS workspace_id "
                "FROM agent_view av JOIN workspace w ON w.id = av.workspace_id "
                "WHERE av.is_active = 1 AND w.is_active = 1"
            )
            view_rows = cur.fetchall()
            cur.execute(
                "SELECT scope, scope_id, path, value FROM core_config_data WHERE path LIKE 'github/%%'"
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
        # Effective resolution is ENV -> DB -> config.json. Only github_owner can still be satisfied by
        # ENV here (the other three are refused outright above), and it is global, so model it as a
        # DEFAULT-scope row — which correctly does NOT satisfy the per-view-scope requirement.
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

        print("\n=== GitHub onboarding ===")
        core_cfg = get_module_config("core")
        toolbox_url = core_cfg.get("toolbox/url", "") if isinstance(core_cfg, dict) else ""
        if not toolbox_url:
            print(
                "  Error: core/toolbox/url not configured. The GitHub token is verified inside the "
                "toolbox, so a reachable toolbox is required to onboard.\n"
                "  Set it (`agento config:set core/toolbox/url <url>`) and re-run, or configure manually "
                "with `agento config:set github/...` (see docs/modules/github.md)."
            )
            return

        # GitHub config is ALWAYS agent_view-scoped (the token must never live at DEFAULT scope — see
        # _VIEW_SCOPED_PATHS), so an active agent_view is required to attribute it to.
        views = get_active_agent_views(conn)
        if not views:
            print(
                "  Error: no active agent_view. GitHub config is per-agent_view (the token is never "
                "stored at default scope), so create/activate an agent_view first, then re-run."
            )
            return

        print(
            "  The token needs these fine-grained repository permissions:\n"
            "    " + "\n    ".join(_REQUIRED_PERMISSIONS) + "\n"
            "  Classic token equivalent: the `repo` scope (`public_repo` for public repositories only)."
        )

        client = GitHubToolboxClient(toolbox_url)
        try:
            # Verify-before-save loop: nothing is written until a credential verifies.
            while True:
                owner_name = input("  GitHub owner (user or organization): ").strip()
                token = getpass.getpass("  GitHub personal access token: ").strip()
                repo_allowlist = input("  Watched repo names (comma-separated): ").strip()

                if not (owner_name and token and repo_allowlist):
                    print("  Error: owner, token and at least one repo are all required.")
                    if terminal.select("How to proceed?", ["Retry", "Abort (nothing saved)"]) == 1:
                        return
                    continue

                try:
                    result = client.verify(token)
                except Exception as e:  # toolbox unreachable / network / non-200
                    print(f"  Error: could not verify via toolbox at {toolbox_url}: {e}")
                    if terminal.select("How to proceed?", ["Retry", "Abort (nothing saved)"]) == 1:
                        return
                    continue

                if result.get("ok"):
                    login = result.get("login") or ""
                    user_id = result.get("id") or ""
                    print(f"  Verified: authenticated as {login} (id {user_id}).")
                    break

                detail = result.get("detail") or f"HTTP {result.get('status')}"
                print(f"  Error: credential verification failed ({detail}). Nothing saved.")
                if terminal.select("How to proceed?", ["Retry", "Abort (nothing saved)"]) == 1:
                    return
        finally:
            client.close()

        # GitHub links a commit to an account only when the author email is a verified email on it — the
        # users.noreply default always links, and needs no address the operator has to verify.
        print(
            "  GitHub links a commit to an account only when the author email is a verified email on it "
            "— the `users.noreply` default always links."
        )
        default_email = f"{user_id}+{login}@users.noreply.github.com"
        email = input(f"  Git commit author email [{default_email}]: ").strip() or default_email

        # Always write at an AGENT_VIEW scope (never DEFAULT): the token must not live at DEFAULT (else
        # bootstrap would decrypt it in cron), and agent_view-scoped login/repo_allowlist keep per-view
        # attribution correct. Auto-select the sole view; prompt when there are several.
        if len(views) > 1:
            idx = terminal.select(
                "Which agent_view owns this GitHub account?",
                [f"{av.code} ({av.label})" for av in views],
            )
            owner_view = views[idx]
        else:
            owner_view = views[0]
        scope, scope_id = Scope.AGENT_VIEW, owner_view.id
        scope_desc = f"agent_view '{owner_view.code}'"

        # Single transaction: commit only AFTER a successful verify.
        scoped_config_set(conn, "github/github_owner", owner_name, scope=scope, scope_id=scope_id)
        scoped_config_set(conn, "github/github_login", login, scope=scope, scope_id=scope_id)
        scoped_config_set(conn, "github/repo_allowlist", repo_allowlist, scope=scope, scope_id=scope_id)
        # Seed this agent_view's git commit identity so its commits link to this GitHub account. The
        # framework materializes ~/.gitconfig [user] from these at workspace:build.
        scoped_config_set(conn, "agent_view/identity/git_author_email", email, scope=scope, scope_id=scope_id)
        scoped_config_set(conn, "agent_view/identity/git_author_name", login, scope=scope, scope_id=scope_id)
        scoped_config_set(
            conn, "github/github_token", token,
            scope=scope, scope_id=scope_id, encrypted=True,
        )
        conn.commit()
        logger.info("GitHub onboarding saved config at scope=%s", scope_desc)

        print(
            f"\n  Saved GitHub config at {scope_desc} scope.\n"
            "  Next steps (GitHub tools are opt-in; nothing acts until enabled):\n"
            "    1) Enable the capabilities you want (one tool per call), e.g.:\n"
            "       for t in github_get_pr github_get_pr_diff github_get_pr_comments \\\n"
            f"                github_add_comment; do agento tool:enable \"$t\" --agent-view {owner_view.code}; done\n"
            "    2) Confirm the watched repos: "
            f"agento config:get github/repo_allowlist\n"
            "    3) Turn the channel on (same scope as the config above):\n"
            f"       agento config:set github/enabled 1 --scope=agent_view --scope-id={owner_view.id}"
        )
