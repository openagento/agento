"""The Python issuer and the Node verifier must agree on what a valid capability is.

Both read ``tests/fixtures/auth_context_v1.json``; the Node half is
``src/agento/toolbox/tests/auth-context-fixture.test.js``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.framework.auth_context import (
    ENDPOINT_TRANSPORT,
    LEGACY_REST_SUBJECT,
    TTL_CEILINGS,
    AuthConfigError,
    compute_auth_ttls,
    derive_auth_context,
)

_FIXTURE = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures" / "auth_context_v1.json").read_text()
)


@pytest.mark.parametrize("case", _FIXTURE["cases"], ids=[c["why"] for c in _FIXTURE["cases"]])
def test_python_derivation_matches_the_shared_fixture(case):
    got = derive_auth_context(
        row=case["row"],
        agent_view_workspace_id=case["agent_view_workspace_id"],
        source=case["source"],
        endpoint=case["endpoint"],
        ttl_caps=_FIXTURE["ttl_caps"],
    )
    assert got == case["expected"]


def test_constants_match_the_shared_fixture():
    assert _FIXTURE["ttl_ceilings"] == TTL_CEILINGS
    assert _FIXTURE["legacy_rest_subject"] == LEGACY_REST_SUBJECT
    assert _FIXTURE["endpoint_transport"] == ENDPOINT_TRANSPORT


@pytest.mark.parametrize(
    "case", _FIXTURE["ttl_resolution"], ids=[c["why"] for c in _FIXTURE["ttl_resolution"]]
)
def test_python_ttl_resolution_matches_the_shared_fixture(case):
    kwargs = dict(
        env=case["env"],
        default_overrides=case["default_overrides"],
        workspace_overrides=case["workspace_overrides"],
        config_defaults=case["config_defaults"],
    )
    if case["expected"] == "error":
        with pytest.raises(AuthConfigError):
            compute_auth_ttls(**kwargs)
    else:
        assert compute_auth_ttls(**kwargs) == case["expected"]
