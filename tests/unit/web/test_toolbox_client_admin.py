"""``admin_request``: one store-admin call from the backend to the toolbox ``/admin`` endpoint."""
from __future__ import annotations

import contextlib

import httpx
import pytest
import respx

from agento.framework.auth_context import derive_auth_context
from agento.web import toolbox_client

URL = "http://toolbox:3001"


@pytest.fixture
def minted(monkeypatch):
    calls: list[dict] = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return contextlib.nullcontext("tok-admin")

    monkeypatch.setattr(toolbox_client, "rest_capability", _fake)
    monkeypatch.setattr(toolbox_client, "resolve_toolbox_url", lambda conn: URL)
    return calls


@respx.mock
def test_mints_one_viewless_capability_and_sends_it_in_the_header(minted):
    route = respx.post(f"{URL}/admin/miniapps/app/resolve").mock(
        return_value=httpx.Response(200, json={"version_id": "v-1"}))
    got = toolbox_client.admin_request(None, "POST", "miniapps/app/resolve", subject_id="service:web:user:3",
                                       body={"version": "current"})
    assert got == toolbox_client.InvokeResult(200, {"version_id": "v-1"})
    assert minted == [{"agent_view_id": None, "subject_id": "service:web:user:3"}]
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer tok-admin"
    assert request.content == b'{"version":"current"}'
    assert "tok-admin" not in str(request.url)


@respx.mock
def test_passes_a_refusal_through_and_maps_transport_failures(minted):
    respx.delete(f"{URL}/admin/versioned-artifacts/site").mock(
        return_value=httpx.Response(404, json={"error_code": "ARTIFACT_NOT_FOUND"}))
    respx.get(f"{URL}/admin/versioned-artifacts").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{URL}/admin/versioned-artifacts/x/versions").mock(return_value=httpx.Response(200, text="<html>"))
    assert toolbox_client.admin_request(None, "DELETE", "versioned-artifacts/site", subject_id="service:cli").status == 404
    assert toolbox_client.admin_request(None, "GET", "versioned-artifacts", subject_id="service:cli").status == 503
    assert toolbox_client.admin_request(None, "GET", "versioned-artifacts/x/versions",
                                        subject_id="service:cli").status == 502


def _row(subject_id, agent_view_id=None):
    return {"id": "9", "kind": "internal_rest", "actor": "service", "subject_id": subject_id,
            "agent_view_id": agent_view_id, "workspace_id": None if agent_view_id is None else 3, "job_id": None,
            "allowed_transports": ["http"], "created_at": 1, "expires_at": 121, "on_behalf_of": None,
            "execution_id": None, "app_artifact_code": None, "app_version_id": None, "app_launch_id": None,
            "tool_ceiling": None, "source_kind": None, "source_id": None}


def test_the_row_it_mints_is_the_one_the_admin_endpoint_admits():
    # An internal_rest subject is `service:…`: a panel user is named inside it, never bare.
    assert derive_auth_context(row=_row("service:web:user:3"), endpoint="admin") is not None
    assert derive_auth_context(row=_row("user:3"), endpoint="admin") is None
    assert derive_auth_context(row=_row("service:web:user:3", 7), agent_view_workspace_id=3, endpoint="admin") is None
