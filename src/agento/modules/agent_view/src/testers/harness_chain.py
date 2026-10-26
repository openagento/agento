"""Config tester for ``agent_view/model``: can a run at this scope start, end to end?

It checks the chain a run depends on, in order, and the first link that does not hold
is the result: the harness is registered, its CLI is installed here, the provider is
one the harness declares, the provider's credential pool has a healthy credential,
and the harness accepts the model id (its optional ``check_model`` member,
docs/architecture/harness-contract.md).

A ``local`` tester: it runs in cron, where the harness CLIs and the credential pool
already are (CFG-3). It writes nothing: the pool is read with ``list_credentials``
and one ``get_credential``, never ``select_credential``, which stamps and leases.
See docs/config/testers.md.
"""
from __future__ import annotations

import shutil
from datetime import UTC, datetime

from agento.framework.agent_manager.credential_store import get_credential, list_credentials
from agento.framework.agent_manager.models import CredentialStatus
from agento.framework.agent_view_runtime import resolve_runtime_for_scope
from agento.framework.config_test import ERROR, FAIL, OK, TestResult, sanitize
from agento.framework.harness import UnknownHarnessError, find_harness

TIMEOUT_S = 30.0


def _healthy(record, now: datetime) -> bool:
    """The filter ``select_credential`` applies, without its write."""
    return (
        record.enabled
        and record.status == CredentialStatus.OK
        and (record.expires_at is None or record.expires_at > now)
        and (record.throttled_until is None or record.throttled_until <= now)
    )


def _selection_order(record) -> tuple:
    """``select_credential``'s ORDER BY: priority, never-used first, used_at, id."""
    return (
        record.priority,
        record.used_at is not None,
        record.used_at or datetime.min,
        record.id,
    )


def _secret_leaves(value, out: dict[str, str]) -> dict[str, str]:
    """Every string in a decrypted payload, under a synthetic key, for ``sanitize``."""
    if isinstance(value, dict):
        for item in value.values():
            _secret_leaves(item, out)
    elif isinstance(value, list):
        for item in value:
            _secret_leaves(item, out)
    elif isinstance(value, str) and value:
        out[f"secret{len(out)}"] = value
    return out


class HarnessChainTester:
    timeout_s = TIMEOUT_S

    def run(self, conn, *, scope: str, scope_id: int) -> TestResult:
        try:
            harness_id, provider_id, model = resolve_runtime_for_scope(conn, scope, scope_id)
        except UnknownHarnessError as e:
            return TestResult(FAIL, str(e.args[0]), code="HARNESS_UNKNOWN")
        except ValueError as e:
            return TestResult(FAIL, str(e), code="PROVIDER_UNKNOWN")

        registered = find_harness(harness_id) if harness_id else None
        if registered is None:
            return TestResult(
                FAIL,
                f"harness {harness_id!r} is not registered — is its module enabled?",
                code="HARNESS_UNKNOWN",
            )
        descriptor = registered.descriptor
        package = descriptor.sandbox_package
        if package is not None and shutil.which(package.binary) is None:
            return TestResult(
                FAIL,
                f"{harness_id}: the {package.binary!r} CLI is not installed here",
                code="HARNESS_NOT_INSTALLED",
            )
        provider = descriptor.provider(provider_id) if provider_id else None
        if provider is None:
            return TestResult(
                FAIL,
                f"harness {harness_id!r} does not declare provider {provider_id!r}",
                code="PROVIDER_UNKNOWN",
            )
        chain = f"{harness_id} · {provider_id}"

        credential = None
        if provider.credential_required:
            now = datetime.now(UTC).replace(tzinfo=None)
            healthy = sorted(
                (r for r in list_credentials(conn, provider.credential_scope,
                                             include_credentials=False)
                 if _healthy(r, now)),
                key=_selection_order,
            )
            if not healthy:
                return TestResult(
                    FAIL,
                    f"{chain}: no healthy credential in pool {provider.credential_scope!r} — "
                    f"bin/agento credential:register {provider.credential_scope} <label>",
                    code="NO_CREDENTIAL",
                )
            label = healthy[0].label
            try:
                credential = get_credential(conn, healthy[0].id)
            except Exception as e:
                # Type name only: a decrypt error can quote what it was handling.
                return TestResult(
                    ERROR,
                    f"{chain}: credential {label!r} could not be decrypted "
                    f"({type(e).__name__}) — is AGENTO_ENCRYPTION_KEY the one it was stored with?",
                    code="DECRYPT_FAILED",
                )
            if credential is None:
                return TestResult(
                    ERROR, f"{chain}: credential {label!r} went away during the check",
                    code="CREDENTIAL_READ_FAILED",
                )
            chain += f" · credential {label!r}"

        if not model:
            return TestResult(
                OK, f"{chain} · no model set, the harness uses its own default",
                code="MODEL_DEFAULT",
            )

        result = self._check_model(registered.adapter, provider_id, model, credential)
        secrets = _secret_leaves(credential.credentials if credential else None, {})
        message = sanitize(f"{chain} · {result.message}", secrets, obscure_paths=secrets.keys())
        return TestResult(result.status, message, code=result.code)

    def _check_model(self, adapter, provider_id: str, model: str, credential) -> TestResult:
        # Optional member, read like `stream_renderer` (harness/protocols.py).
        check = getattr(adapter, "check_model", None)
        if not callable(check):
            return TestResult(
                ERROR, f"model {model} not checked: this harness cannot check models",
                code="MODEL_NOT_CHECKED",
            )
        try:
            result = check(provider_id, model, credential, timeout_s=self.timeout_s)
        except Exception as e:
            return TestResult(
                ERROR, f"model {model} not checked: the check raised {type(e).__name__}",
                code="MODEL_CHECK_FAILED",
            )
        if result is None:
            return TestResult(
                ERROR,
                f"model {model} not checked: this harness has no model catalogue "
                f"for provider {provider_id}",
                code="MODEL_NOT_CHECKED",
            )
        return result
