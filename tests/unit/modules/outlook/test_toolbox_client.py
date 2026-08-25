import json as _json

import httpx
import pytest
import respx

from agento.modules.outlook.src.toolbox_client import (
    OutlookToolboxClient,
    ToolboxAPIError,
)


@respx.mock
def test_list_delta_returns_full_payload_with_mailbox_and_delta_link():
    respx.post("http://toolbox:3001/api/outlook/delta").mock(
        return_value=httpx.Response(200, json={"mailbox": "dev@example.com", "deltaLink": "L", "resynced": False, "messages": [{
            "id": "m1", "subject": "A",
            "from": {"name": "X", "address": "x@y.com"},
            "receivedDateTime": "2026-01-01T00:00:00Z",
            "conversationId": "c1", "dmarc": "pass",
        }]})
    )
    client = OutlookToolboxClient("http://toolbox:3001", capability_token="test-cap")
    resp = client.list_delta(top=10)
    client.close()
    assert resp["mailbox"] == "dev@example.com"
    assert resp["deltaLink"] == "L"
    assert resp["messages"][0]["id"] == "m1"
    assert resp["messages"][0]["dmarc"] == "pass"


@respx.mock
def test_list_delta_sends_top_agent_view_id_and_cursors_in_body():
    captured = {}

    def _handler(request):
        captured.update(_json.loads(request.content))
        return httpx.Response(200, json={"mailbox": "dev@example.com", "deltaLink": "L", "resynced": False, "messages": []})

    respx.post("http://toolbox:3001/api/outlook/delta").mock(side_effect=_handler)
    client = OutlookToolboxClient("http://toolbox:3001", capability_token="test-cap")
    resp = client.list_delta(top=7, agent_view_id=5, cursors={"dev@example.com": "PREV"})
    client.close()
    assert captured == {"top": 7, "agent_view_id": 5, "cursors": {"dev@example.com": "PREV"}}
    assert resp["deltaLink"] == "L"


@respx.mock
def test_list_delta_omits_agent_view_id_when_none_and_defaults_cursors():
    captured = {}

    def _handler(request):
        captured.update(_json.loads(request.content))
        return httpx.Response(200, json={"mailbox": None, "deltaLink": None, "resynced": False, "messages": []})

    respx.post("http://toolbox:3001/api/outlook/delta").mock(side_effect=_handler)
    client = OutlookToolboxClient("http://toolbox:3001", capability_token="test-cap")
    resp = client.list_delta(top=3)
    client.close()
    assert captured == {"top": 3, "cursors": {}}
    assert "agent_view_id" not in captured
    assert resp["mailbox"] is None


@respx.mock
def test_list_delta_raises_on_non_200():
    respx.post("http://toolbox:3001/api/outlook/delta").mock(
        return_value=httpx.Response(502, text="boom")
    )
    client = OutlookToolboxClient("http://toolbox:3001", capability_token="test-cap")
    with pytest.raises(ToolboxAPIError) as exc:
        client.list_delta(top=1, agent_view_id=9, cursors={})
    client.close()
    assert exc.value.status_code == 502
