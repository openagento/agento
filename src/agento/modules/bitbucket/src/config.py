from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BitbucketConfig:
    """Python-side Bitbucket config — NO API token.

    The Atlassian API token (``bitbucket_api_token``) lives in ``system.json`` ONLY so the TOOLBOX (the
    zero-trust boundary — "toolbox = the only container holding the tool credential store") can resolve and decrypt it. It is
    deliberately NOT a field here, exactly as ``OutlookConfig`` omits the Graph secrets and ``JiraConfig``
    omits ``jira_token``: this dataclass is what the publisher carries, so the token is never part of it.

    The token is decrypted ONLY inside the toolbox. Two things guarantee the cron/publisher never decrypts
    it: (1) Bitbucket config is ALWAYS agent_view-scoped — the token is never stored at DEFAULT scope (see
    onboarding.py / docs), so the framework's ``bootstrap()``, which resolves DEFAULT-scope obscure config,
    never sees it; (2) the publisher resolves only the non-secret paths it needs (see ``commands/_loop.py``
    — per-path ``ScopedConfigService.get()``, never ``get_module()``, which would resolve the token field).
    The agent never receives the token in any case.

    ``bitbucket_email`` is the Basic-auth username — also toolbox-only; the publisher never sends it
    anywhere (every Bitbucket API call goes through the toolbox), so it is omitted here too.
    """

    enabled: bool = False
    workspace: str = ""
    account_uuid: str = ""
    repo_allowlist: str = ""
    poll_top: int = 20

    @classmethod
    def from_dict(cls, data: dict) -> BitbucketConfig:
        # 3-level config (ENV/DB) returns STRINGS, so bool("0")/bool("false") would be True.
        # Mirror OutlookConfig.from_dict's truthiness set; None (field unset) is treated as disabled.
        enabled_raw = data.get("enabled", False)
        enabled = enabled_raw not in (False, 0, "0", "false", "False", None)
        # poll_top arrives as str/int/garbage — parse defensively and clamp to Bitbucket's 1..50 contract.
        poll_top_raw = data.get("poll_top", 20)
        if poll_top_raw is None:
            poll_top_raw = 20
        try:
            poll_top = int(poll_top_raw)
        except (TypeError, ValueError):
            poll_top = 20
        poll_top = min(max(poll_top, 1), 50)
        return cls(
            enabled=enabled,
            workspace=data.get("bitbucket_workspace", "") or "",
            account_uuid=data.get("bitbucket_account_uuid", "") or "",
            repo_allowlist=data.get("repo_allowlist", "") or "",
            poll_top=poll_top,
        )

    @property
    def repo_list(self) -> list[str]:
        """Watched repo slugs — split, trimmed, de-duped (order preserved)."""
        seen: dict[str, None] = {}
        for raw in self.repo_allowlist.split(","):
            slug = raw.strip()
            if slug and slug not in seen:
                seen[slug] = None
        return list(seen)

    @property
    def toolbox_url(self) -> str:
        from agento.framework.bootstrap import get_module_config

        core_cfg = get_module_config("core")
        if isinstance(core_cfg, dict):
            return core_cfg.get("toolbox/url", "") or ""
        return ""
