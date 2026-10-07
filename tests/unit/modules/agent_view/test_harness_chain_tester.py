"""``HarnessChainTester``: one test per row of the result table in docs/config/testers.md."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from agento.framework.agent_manager.models import CredentialRecord, CredentialStatus
from agento.framework.config_test import ERROR, FAIL, OK, TestResult
from agento.framework.harness import UnknownHarnessError
from agento.framework.harness.descriptor import HarnessDescriptor
from agento.modules.agent_view.src.testers import harness_chain
from agento.modules.agent_view.src.testers.harness_chain import HarnessChainTester

NOW = datetime(2026, 10, 7, 12, 0, 0)
DECL = {
    "id": "fake",
    "label": "Fake",
    "default_provider": "cloud",
    "sandbox_package": {"manager": "npm", "package": "fake-cli", "binary": "fake",
                        "version_env_key": "FAKE_VERSION", "default_range": "1.0.0"},
    "providers": [
        {"id": "cloud", "label": "Cloud", "credential_required": True,
         "credential_scope": "fakecloud", "registration_modes": ["api_key"]},
        {"id": "local", "label": "Local", "credential_required": False},
    ],
}


def _record(id, *, label=None, priority=0, used_at=None, status=CredentialStatus.OK,
            enabled=True, expires_at=None, throttled_until=None, credentials=None):
    return CredentialRecord(
        id=id, scope="fakecloud", type="api_key", label=label or f"c{id}",
        credentials=credentials, enabled=enabled, status=status, priority=priority,
        expires_at=expires_at, used_at=used_at, throttled_until=throttled_until,
    )


class _Adapter:
    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.calls = result, raises, []

    def check_model(self, provider, model, credential, *, timeout_s):
        self.calls.append((provider, model, credential, timeout_s))
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture
def env(monkeypatch):
    state = SimpleNamespace(
        runtime=("fake", "cloud", "m-1"),
        adapter=_Adapter(TestResult(OK, "model m-1 found", code="MODEL_OK")),
        registered=True,
        installed=True,
        pool=[_record(1)],
        payloads={1: {"api_key": "sk-secret-value"}},
        decrypted=[],
    )

    def resolve(_conn, _scope, _id):
        if isinstance(state.runtime, Exception):
            raise state.runtime
        return state.runtime

    def find(_id):
        if not state.registered:
            return None
        return SimpleNamespace(descriptor=HarnessDescriptor.from_declaration(DECL), adapter=state.adapter)

    def list_creds(_conn, scope, enabled_only=True, *, include_credentials=True):
        assert include_credentials is False, "the pool is listed without payloads"
        assert scope == "fakecloud"
        return [r for r in state.pool if r.enabled or not enabled_only]

    def get_cred(_conn, cid):
        state.decrypted.append(cid)
        payload = state.payloads[cid]
        if isinstance(payload, Exception):
            raise payload
        return _record(cid, credentials=payload)

    def no_select(*_a, **_k):
        raise AssertionError("select_credential writes; a tester must not call it")

    monkeypatch.setattr(harness_chain, "resolve_runtime_for_scope", resolve)
    monkeypatch.setattr(harness_chain, "find_harness", find)
    monkeypatch.setattr(harness_chain, "list_credentials", list_creds)
    monkeypatch.setattr(harness_chain, "get_credential", get_cred)
    monkeypatch.setattr(
        "agento.framework.agent_manager.credential_store.select_credential", no_select,
    )
    monkeypatch.setattr(harness_chain.shutil, "which", lambda b: f"/usr/bin/{b}" if state.installed else None)
    monkeypatch.setattr(harness_chain, "datetime", _FrozenDatetime)
    return state


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.replace(tzinfo=tz)


def _run():
    return HarnessChainTester().run(object(), scope="agent_view", scope_id=1)


def test_unregistered_harness_during_resolution(env):
    env.runtime = UnknownHarnessError("No harness registered under 'gone'.")
    assert (_run().status, _run().code) == (FAIL, "HARNESS_UNKNOWN")


def test_invalid_provider_during_resolution(env):
    env.runtime = ValueError("Harness 'fake' does not offer provider 'x'.")
    assert (_run().status, _run().code) == (FAIL, "PROVIDER_UNKNOWN")


def test_harness_not_registered(env):
    env.registered = False
    assert (_run().status, _run().code) == (FAIL, "HARNESS_UNKNOWN")


def test_cli_not_installed(env):
    env.installed = False
    result = _run()
    assert (result.status, result.code) == (FAIL, "HARNESS_NOT_INSTALLED")
    assert "'fake'" in result.message


def test_provider_not_declared(env):
    env.runtime = ("fake", "nope", "m-1")
    assert (_run().status, _run().code) == (FAIL, "PROVIDER_UNKNOWN")


def test_no_healthy_credential(env):
    env.pool = [
        _record(1, status=CredentialStatus.ERROR),
        _record(2, expires_at=NOW - timedelta(seconds=1)),
        _record(3, throttled_until=NOW + timedelta(minutes=5)),
        _record(4, enabled=False),
    ]
    result = _run()
    assert (result.status, result.code) == (FAIL, "NO_CREDENTIAL")
    assert env.decrypted == []


def test_only_the_first_healthy_row_in_selection_order_is_decrypted(env):
    env.pool = [
        _record(1, priority=5),
        _record(2, priority=0, used_at=NOW - timedelta(hours=1)),
        _record(3, priority=0, status=CredentialStatus.ERROR),
        _record(4, priority=0, used_at=None, label="fresh"),
    ]
    env.payloads = {4: {"api_key": "k"}}

    result = _run()

    assert env.decrypted == [4]
    assert result.code == "MODEL_OK"
    assert "credential 'fresh'" in result.message


def test_decrypt_failure_is_error_not_missing(env):
    env.payloads = {1: ValueError("bad padding near sk-secret")}
    result = _run()
    assert (result.status, result.code) == (ERROR, "DECRYPT_FAILED")
    assert "sk-secret" not in result.message


def test_empty_model_uses_harness_default(env):
    env.runtime = ("fake", "cloud", "")
    result = _run()
    assert (result.status, result.code) == (OK, "MODEL_DEFAULT")
    assert env.adapter.calls == []


def test_adapter_without_check_model_is_not_checked(env):
    env.adapter = object()
    result = _run()
    assert (result.status, result.code) == (ERROR, "MODEL_NOT_CHECKED")


def test_check_model_returning_none_is_not_checked(env):
    env.adapter = _Adapter(None)
    env.runtime = ("fake", "local", "m-1")
    result = _run()
    assert (result.status, result.code) == (ERROR, "MODEL_NOT_CHECKED")
    assert env.adapter.calls == [("local", "m-1", None, harness_chain.TIMEOUT_S)]


def test_check_model_result_is_passed_through(env):
    env.adapter = _Adapter(TestResult(FAIL, "model m-1 unknown; did you mean: m-2", code="MODEL_UNKNOWN"))
    result = _run()
    assert (result.status, result.code) == (FAIL, "MODEL_UNKNOWN")
    assert result.message.startswith("fake · cloud · credential 'c1' · model m-1 unknown")


def test_check_model_raising_reports_type_name_only(env):
    env.adapter = _Adapter(raises=RuntimeError("leaked sk-secret-value"))
    result = _run()
    assert (result.status, result.code) == (ERROR, "MODEL_CHECK_FAILED")
    assert "RuntimeError" in result.message and "sk-secret" not in result.message


def test_credential_values_are_masked_nested_and_short(env):
    env.payloads = {1: {"api_key": "abc", "raw_auth": {"tokens": [{"t": "nested-token-1"}]}}}
    env.adapter = _Adapter(TestResult(ERROR, "echo abc and nested-token-1", code="MODEL_CHECK_FAILED"))
    result = _run()
    assert "abc" not in result.message
    assert "nested-token-1" not in result.message
    assert "***" in result.message
