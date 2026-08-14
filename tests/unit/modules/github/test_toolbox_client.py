import httpx
import pytest
import respx

from agento.modules.github.src.toolbox_client import GitHubToolboxClient, ToolboxAPIError

BASE = "http://toolbox:3001"


@respx.mock
def test_verify_posts_only_the_token_and_returns_the_body():
    import json
    route = respx.post(f"{BASE}/api/github/verify").mock(
        return_value=httpx.Response(200, json={"ok": True, "login": "agent-bot", "id": 42})
    )
    client = GitHubToolboxClient(BASE)
    try:
        assert client.verify("ghp_x")["login"] == "agent-bot"
    finally:
        client.close()
    # No api_base / host field: the toolbox hardcodes api.github.com, so no caller can redirect it.
    assert json.loads(route.calls.last.request.content) == {"token": "ghp_x"}


@respx.mock
def test_verify_raises_on_non_200():
    respx.post(f"{BASE}/api/github/verify").mock(return_value=httpx.Response(500, text="boom"))
    client = GitHubToolboxClient(BASE)
    try:
        with pytest.raises(ToolboxAPIError) as e:
            client.verify("ghp_x")
        assert e.value.status_code == 500
    finally:
        client.close()


@respx.mock
def test_open_prs_sends_lane_and_optional_top_only():
    import json
    route = respx.post(f"{BASE}/api/github/open-prs").mock(
        return_value=httpx.Response(200, json={"pull_requests": [], "errors": []})
    )
    client = GitHubToolboxClient(BASE)
    try:
        client.open_prs(3, lane="comments")
        assert json.loads(route.calls.last.request.content) == {"agent_view_id": 3, "lane": "comments"}
        client.open_prs(3, lane="changes", top=5)
        assert json.loads(route.calls.last.request.content) == {"agent_view_id": 3, "lane": "changes", "top": 5}
    finally:
        client.close()


@respx.mock
def test_open_prs_never_sends_a_token():
    import json
    route = respx.post(f"{BASE}/api/github/open-prs").mock(
        return_value=httpx.Response(200, json={"pull_requests": [], "errors": []})
    )
    client = GitHubToolboxClient(BASE)
    try:
        client.open_prs(3, lane="comments")
    finally:
        client.close()
    assert "token" not in json.loads(route.calls.last.request.content)
