from __future__ import annotations

import inspect

import httpx
import pytest
import respx

from agento.modules.jira.src.toolbox_client import ToolboxAPIError, ToolboxClient


@respx.mock
def test_jira_search_success(jira_todo):
    respx.post("http://toolbox:3001/api/jira/search").mock(
        return_value=httpx.Response(200, json=jira_todo)
    )

    client = ToolboxClient("http://toolbox:3001", capability_token="test-cap")
    result = client.jira_search(
        jql='project = AI AND status = "To Do"',
        fields=["key", "summary"],
    )

    assert len(result["issues"]) == 2
    assert result["issues"][0]["key"] == "AI-10"


@respx.mock
def test_jira_search_sends_correct_payload():
    route = respx.post("http://toolbox:3001/api/jira/search").mock(
        return_value=httpx.Response(200, json={"issues": []})
    )

    client = ToolboxClient("http://toolbox:3001", capability_token="test-cap")
    client.jira_search(jql="project = X", fields=["key"], max_results=10)

    request = route.calls[0].request
    body = request.content.decode()
    import json
    payload = json.loads(body)
    assert payload["jql"] == "project = X"
    assert payload["fields"] == ["key"]
    assert payload["maxResults"] == 10


@respx.mock
def test_jira_search_http_error():
    respx.post("http://toolbox:3001/api/jira/search").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    client = ToolboxClient("http://toolbox:3001", capability_token="test-cap")
    with pytest.raises(ToolboxAPIError) as exc_info:
        client.jira_search(jql="bad", fields=[])

    assert exc_info.value.status_code == 500
    assert "Internal Server Error" in exc_info.value.body


@respx.mock
def test_jira_request_success():
    respx.post("http://toolbox:3001/api/jira/request").mock(
        return_value=httpx.Response(200, json={
            "ok": True, "status": 200, "data": [{"id": "f1", "name": "Summary"}],
        })
    )

    client = ToolboxClient("http://toolbox:3001", capability_token="test-cap")
    result = client.jira_request("GET", "/rest/api/3/field")

    assert result == [{"id": "f1", "name": "Summary"}]


@respx.mock
def test_jira_request_jira_error():
    respx.post("http://toolbox:3001/api/jira/request").mock(
        return_value=httpx.Response(200, json={
            "ok": False, "status": 403, "data": {"errorMessages": ["Forbidden"]},
        })
    )

    client = ToolboxClient("http://toolbox:3001", capability_token="test-cap")
    with pytest.raises(ToolboxAPIError) as exc_info:
        client.jira_request("POST", "/rest/api/3/field", {"name": "Test"})

    assert exc_info.value.status_code == 403


@respx.mock
def test_jira_request_toolbox_error():
    respx.post("http://toolbox:3001/api/jira/request").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    client = ToolboxClient("http://toolbox:3001", capability_token="test-cap")
    with pytest.raises(ToolboxAPIError) as exc_info:
        client.jira_request("GET", "/rest/api/3/myself")

    assert exc_info.value.status_code == 500


def test_jira_request_has_no_host_override():
    """The proxy decides the host from config; the client must not offer a knob for it."""
    params = inspect.signature(ToolboxClient.jira_request).parameters
    assert "jira_host" not in params


def test_a_client_built_without_a_capability_is_rejected_at_construction():
    """Every /api route is guarded, so a tokenless client can only make a request that is
    already known to fail 401 — and it fails far from the code that knows which view owns the
    call. Constructing one is the bug; that is where it must be caught."""
    with pytest.raises(ValueError, match="capability_token is required"):
        ToolboxClient("http://toolbox:3001", capability_token="")
