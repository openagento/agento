from __future__ import annotations

import httpx


class ToolboxAPIError(Exception):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Toolbox API HTTP {status_code}: {body}")


class GitHubToolboxClient:
    """HTTP client for the toolbox GitHub REST API.

    The publisher holds NO GitHub credential — it asks the toolbox (the only token holder) to talk to
    GitHub on its behalf. The toolbox resolves and enforces the scoped owner/login/allow-list; the
    publisher only passes ``agent_view_id`` (which view to act as) and a lane.
    """

    def __init__(self, base_url: str, timeout: float = 60.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def verify(self, token: str) -> dict:
        """Verify a PAT against ``GET /user`` (used by onboarding, before any save).

        Returns the parsed body ``{ok, login?, id?, status?, detail?}``. Raises only on a toolbox-level
        (non-200) failure; an auth failure surfaces as ``{ok: false, ...}`` with HTTP 200. The GitHub
        host is NOT a parameter — the toolbox hardcodes it, so this endpoint cannot be aimed elsewhere.
        """
        response = self._client.post("/api/github/verify", json={"token": token})
        if response.status_code != 200:
            raise ToolboxAPIError(response.status_code, response.text)
        return response.json()

    def open_prs(self, agent_view_id: int, *, lane: str, top: int | None = None) -> dict:
        """Fetch the agent's OPEN PRs (per the scoped allow-list) for a lane.

        ``top`` is an optional NARROWING limit (never authorization). Returns
        ``{pull_requests: [...], errors: [...]}``.
        """
        body: dict = {"agent_view_id": agent_view_id, "lane": lane}
        if top is not None:
            body["top"] = top
        response = self._client.post("/api/github/open-prs", json=body)
        if response.status_code != 200:
            raise ToolboxAPIError(response.status_code, response.text)
        return response.json()

    def close(self) -> None:
        self._client.close()
