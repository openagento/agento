import json

import pytest
import respx
from httpx import Response

from agento.modules.bitbucket.src.toolbox_client import BitbucketToolboxClient, ToolboxAPIError

BASE = "http://toolbox:3001"


@respx.mock
def test_verify_posts_creds_and_returns_body():
    route = respx.post(f"{BASE}/api/bitbucket/verify").mock(
        return_value=Response(200, json={"ok": True, "account_uuid": "{a}", "username": "agent"})
    )
    client = BitbucketToolboxClient(BASE, capability_token="test-cap")
    try:
        result = client.verify("acme", "e@x.com", "tok")
    finally:
        client.close()
    assert result["ok"] is True
    assert result["account_uuid"] == "{a}"
    assert json.loads(route.calls[0].request.content) == {
        "workspace": "acme", "email": "e@x.com", "api_token": "tok",
    }


@respx.mock
def test_open_prs_posts_agent_view_id_lane_and_top():
    route = respx.post(f"{BASE}/api/bitbucket/open-prs").mock(
        return_value=Response(200, json={"pull_requests": [{"id": 1}], "errors": []})
    )
    client = BitbucketToolboxClient(BASE, capability_token="test-cap")
    try:
        result = client.open_prs(7, lane="changes", top=5)
    finally:
        client.close()
    assert result == {"pull_requests": [{"id": 1}], "errors": []}
    assert json.loads(route.calls[0].request.content) == {
        "agent_view_id": 7, "lane": "changes", "top": 5,
    }


@respx.mock
def test_open_prs_omits_top_when_none():
    route = respx.post(f"{BASE}/api/bitbucket/open-prs").mock(
        return_value=Response(200, json={"pull_requests": [], "errors": []})
    )
    client = BitbucketToolboxClient(BASE, capability_token="test-cap")
    try:
        client.open_prs(7, lane="comments")
    finally:
        client.close()
    assert "top" not in json.loads(route.calls[0].request.content)


@respx.mock
def test_non_200_raises_toolbox_api_error():
    respx.post(f"{BASE}/api/bitbucket/open-prs").mock(return_value=Response(503, text="upstream down"))
    client = BitbucketToolboxClient(BASE, capability_token="test-cap")
    try:
        with pytest.raises(ToolboxAPIError) as exc:
            client.open_prs(7, lane="comments")
        assert exc.value.status_code == 503
    finally:
        client.close()
