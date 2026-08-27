"""The toolbox arm: validate the URL, call /config-test, map the four states."""
from __future__ import annotations

import httpx
import pytest
import respx

from agento.framework.config_test import ERROR, FAIL, NOT_CONFIGURED, OK
from agento.framework.config_test.toolbox import (
    DEFAULT_TOOLBOX_URL,
    ToolboxUrlError,
    _origin,
    _validated_toolbox_url,
    run_toolbox_test,
)

PATH = "core/smtp_pass"


@pytest.fixture
def url(monkeypatch):
    """Pin the endpoint without a DB — the resolver is exercised separately."""
    monkeypatch.setattr(
        "agento.framework.config_test.toolbox._resolve_toolbox_url",
        lambda conn: DEFAULT_TOOLBOX_URL,
    )
    return DEFAULT_TOOLBOX_URL


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://toolbox:3001", "http://toolbox:3001"),
        ("https://toolbox:3001/", "https://toolbox:3001"),
        ("  http://toolbox:3001/health?x=1  ", "http://toolbox:3001"),
        ("http://toolbox", "http://toolbox"),
    ],
)
def test_a_usable_url_keeps_only_its_authority(raw, expected):
    assert _validated_toolbox_url(raw) == expected


@pytest.mark.parametrize(
    "raw,fragment",
    [
        ("file:///etc/passwd", "must be http"),
        ("gopher://x", "must be http"),
        ("http://user:pw@toolbox:3001", "must not embed credentials"),
        ("http:///nohost", "no host"),
        ("http://toolbox:abc", "non-numeric port"),
        ("http://[::1", "not a parsable URL"),
        ("", "must be http"),
    ],
)
def test_an_unusable_url_is_refused(raw, fragment):
    with pytest.raises(ToolboxUrlError) as exc:
        _validated_toolbox_url(raw)
    assert fragment in str(exc.value)


def test_printing_a_malformed_url_does_not_raise():
    """`_origin` is called from every error path, including ones handed a URL
    that never went through validation. A printing helper that raises turns a
    diagnostic into a traceback."""
    assert _origin("http://[::1") == "<unprintable url>"
    assert _origin("http://toolbox:abc") == "http://toolbox"
    assert _origin("http://toolbox:3001") == "http://toolbox:3001"
    assert "pw" not in _origin("http://user:pw@toolbox:3001")


def test_an_invalid_configured_url_is_an_error(monkeypatch):
    monkeypatch.setattr(
        "agento.framework.config_test.toolbox._resolve_toolbox_url",
        lambda conn: (_ for _ in ()).throw(ToolboxUrlError("core/toolbox/url must be http://")),
    )
    r = run_toolbox_test(None, PATH, scope="default")
    assert (r.status, r.code) == (ERROR, "TOOLBOX_URL_INVALID")


@respx.mock
def test_ok_is_mapped_through(url):
    respx.post(f"{url}/config-test").mock(
        return_value=httpx.Response(200, json={"status": "ok", "code": "OK", "detail": "", "ms": 42})
    )
    r = run_toolbox_test(None, PATH, scope="default")
    assert r.status == OK
    assert "42 ms" in r.message


@respx.mock
def test_fail_carries_the_code_and_detail(url):
    respx.post(f"{url}/config-test").mock(
        return_value=httpx.Response(200, json={
            "status": "fail", "code": "AUTH_FAILED", "detail": "535 5.7.8 authentication failed",
        })
    )
    r = run_toolbox_test(None, PATH, scope="default")
    assert (r.status, r.code) == (FAIL, "AUTH_FAILED")
    assert "535" in r.message


@respx.mock
def test_not_configured_is_mapped(url):
    respx.post(f"{url}/config-test").mock(
        return_value=httpx.Response(200, json={
            "status": "not_configured", "code": "NOT_SET", "detail": "'host' is empty",
        })
    )
    r = run_toolbox_test(None, PATH, scope="default")
    assert r.status == NOT_CONFIGURED


@respx.mock
def test_a_toolbox_side_error_stays_an_error(url):
    respx.post(f"{url}/config-test").mock(
        return_value=httpx.Response(200, json={
            "status": "error", "code": "DECRYPT_FAILED", "detail": "stored but unreadable",
        })
    )
    r = run_toolbox_test(None, PATH, scope="default")
    assert (r.status, r.code) == (ERROR, "DECRYPT_FAILED")


@respx.mock
def test_the_agent_view_scope_is_passed_as_a_query_parameter(url):
    route = respx.post(f"{url}/config-test").mock(
        return_value=httpx.Response(200, json={"status": "ok", "code": "OK"})
    )
    run_toolbox_test(None, "agent_view/x", scope="agent_view", scope_id=7)
    request = route.calls[0].request
    assert request.url.params["path"] == "agent_view/x"
    assert request.url.params["agent_view_id"] == "7"


@respx.mock
def test_the_default_scope_sends_no_agent_view_id(url):
    route = respx.post(f"{url}/config-test").mock(
        return_value=httpx.Response(200, json={"status": "ok", "code": "OK"})
    )
    run_toolbox_test(None, PATH, scope="default")
    assert "agent_view_id" not in route.calls[0].request.url.params


@respx.mock
def test_an_unreachable_toolbox_is_an_error_not_a_failure(url):
    """A healthy credential must never read as broken because the container is
    down or the compose name does not resolve on this host."""
    respx.post(f"{url}/config-test").mock(side_effect=httpx.ConnectError("nope"))
    r = run_toolbox_test(None, PATH, scope="default")
    assert (r.status, r.code) == (ERROR, "TOOLBOX_UNREACHABLE")
    assert "toolbox:3001" in r.message
    assert "--local" in r.message          # tells the operator what to try


@respx.mock
def test_a_timeout_is_an_error(url):
    respx.post(f"{url}/config-test").mock(side_effect=httpx.ReadTimeout("slow"))
    r = run_toolbox_test(None, PATH, scope="default")
    assert (r.status, r.code) == (ERROR, "TOOLBOX_UNREACHABLE")


@respx.mock
def test_a_non_200_is_an_error(url):
    respx.post(f"{url}/config-test").mock(return_value=httpx.Response(503, text="nope"))
    r = run_toolbox_test(None, PATH, scope="default")
    assert (r.status, r.code) == (ERROR, "TOOLBOX_HTTP_ERROR")


@respx.mock
@pytest.mark.parametrize("body", ["not json", "[]", '"a string"', "null"])
def test_a_body_that_is_not_a_json_object_is_an_error(url, body):
    respx.post(f"{url}/config-test").mock(return_value=httpx.Response(200, text=body))
    r = run_toolbox_test(None, PATH, scope="default")
    assert (r.status, r.code) == (ERROR, "TOOLBOX_BAD_BODY")


@respx.mock
def test_an_unknown_status_is_an_error(url):
    respx.post(f"{url}/config-test").mock(
        return_value=httpx.Response(200, json={"status": "weird"})
    )
    r = run_toolbox_test(None, PATH, scope="default")
    assert (r.status, r.code) == (ERROR, "TOOLBOX_BAD_BODY")


@respx.mock
def test_a_code_of_the_wrong_shape_is_dropped_not_printed(url):
    """`code` is returned unsanitized, so its shape is enforced on the way in —
    otherwise an upstream error string could ride in on it."""
    respx.post(f"{url}/config-test").mock(
        return_value=httpx.Response(200, json={
            "status": "fail", "code": "535 rejected hunter2xyz", "detail": "no",
        })
    )
    r = run_toolbox_test(None, PATH, scope="default")
    assert r.code == ""
    assert "hunter2xyz" not in r.message


@respx.mock
def test_a_multiline_detail_is_flattened(url):
    respx.post(f"{url}/config-test").mock(
        return_value=httpx.Response(200, json={
            "status": "fail", "code": "X", "detail": "line one\nline two",
        })
    )
    r = run_toolbox_test(None, PATH, scope="default")
    assert "\n" not in r.message


def test_an_agent_view_scope_without_a_positive_id_is_refused():
    """`scope_id=0` used to drop the `agent_view_id` parameter, so the toolbox
    probed the DEFAULT credential and the caller was told `ok` about a scope
    that was never read. `agent_view.id` is auto_increment: 0 names nothing."""
    for bad in (0, -1, None, "3"):
        r = run_toolbox_test(None, PATH, scope="agent_view", scope_id=bad)
        assert (r.status, r.code) == (ERROR, "SCOPE_ID_INVALID"), bad
