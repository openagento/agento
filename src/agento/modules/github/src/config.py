from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GitHubConfig:
    """Python-side GitHub config — NO access token.

    The PAT (``github_token``) lives in ``system.json`` ONLY so the TOOLBOX (the zero-trust boundary)
    can resolve and decrypt it. It is deliberately NOT a field here, exactly as ``BitbucketConfig``
    omits ``bitbucket_api_token``: this dataclass is what the publisher carries, so the token is never
    part of it. The STORED token is never decrypted here: ``load_db_overrides`` (the DB source
    ``bootstrap()`` resolves from) selects ``scope='default'`` rows only, and ``showInDefault: false``
    keeps ``github/github_token`` out of DEFAULT — so an agent_view row is invisible to bootstrap. The
    publisher additionally resolves only the non-secret paths via per-path ``ScopedConfigService.get()``,
    never ``get_module()``. The one gap is a global ``CONFIG__GITHUB__GITHUB_TOKEN``, which
    ``resolve_field`` returns before the DB and before any module code runs; ``env_guard`` makes the
    module refuse to run and refuse to report complete in that state (see Global Constraints).
    """

    enabled: bool = False
    owner: str = ""
    login: str = ""
    repo_allowlist: str = ""
    poll_top: int = 20

    @classmethod
    def from_dict(cls, data: dict) -> GitHubConfig:
        # 3-level config (ENV/DB) returns STRINGS, so bool("0")/bool("false") would be True.
        enabled_raw = data.get("enabled", False)
        enabled = enabled_raw not in (False, 0, "0", "false", "False", None)
        poll_top_raw = data.get("poll_top", 20)
        if poll_top_raw is None:
            poll_top_raw = 20
        try:
            poll_top = int(poll_top_raw)
        except (TypeError, ValueError):
            poll_top = 20
        # GitHub's per_page maximum is 100 (Bitbucket's pagelen cap was 50).
        poll_top = min(max(poll_top, 1), 100)
        return cls(
            enabled=enabled,
            owner=data.get("github_owner", "") or "",
            login=data.get("github_login", "") or "",
            repo_allowlist=data.get("repo_allowlist", "") or "",
            poll_top=poll_top,
        )

    @property
    def repo_list(self) -> list[str]:
        """Watched repo names — split, trimmed, de-duped (order preserved)."""
        seen: dict[str, None] = {}
        for raw in self.repo_allowlist.split(","):
            name = raw.strip()
            if name and name not in seen:
                seen[name] = None
        return list(seen)
