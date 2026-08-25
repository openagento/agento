from __future__ import annotations

import httpx


class ToolboxAPIError(Exception):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Toolbox API HTTP {status_code}: {body}")


class BitbucketToolboxClient:
    """HTTP client for the toolbox Bitbucket REST API.

    The publisher holds NO Bitbucket credential — it asks the toolbox (the only token holder) to talk to
    Bitbucket on its behalf. The toolbox resolves and enforces the scoped workspace/account/allow-list;
    the publisher only passes ``agent_view_id`` (which view to act as) and a lane.
    """

    def __init__(
        self, base_url: str, timeout: float = 60.0, *, capability_token: str
    ):
        # Every /api route is capability-guarded. The token rides in a header, never in
        # the URL or the body: a URL reaches access logs, and a body claim is only ever
        # cross-checked against the capability — never trusted on its own.
        # REQUIRED, not optional. Every /api route is capability-guarded, so a client built
        # without a token can only make a request that is already known to fail with 401 —
        # better to fail here, where the missing scope owner is still visible, than at the
        # far end of an HTTP call.
        if not capability_token:
            raise ValueError("capability_token is required — every /api route is guarded")
        headers = {"Authorization": f"Bearer {capability_token}"}
        self._client = httpx.Client(base_url=base_url, timeout=timeout, headers=headers)

    def verify(self, workspace: str, email: str, api_token: str) -> dict:
        """Verify Basic-auth creds against ``GET /2.0/user`` (used by onboarding, before any save).

        Returns the parsed body ``{ok, account_uuid?, username?, status?, detail?}``. Raises only on a
        toolbox-level (non-200) failure; an auth failure surfaces as ``{ok: false, ...}`` with HTTP 200.
        """
        response = self._client.post(
            "/api/bitbucket/verify",
            json={"workspace": workspace, "email": email, "api_token": api_token},
        )
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
        response = self._client.post("/api/bitbucket/open-prs", json=body)
        if response.status_code != 200:
            raise ToolboxAPIError(response.status_code, response.text)
        return response.json()

    def close(self) -> None:
        self._client.close()
