"""AG-65 out-of-band refresh against the REAL CLI and the REAL provider.

For each OAuth scope (Claude, Codex) the test takes the first healthy OAuth credential of
the running dev stack and moves its stored expiry INTO the refresh horizon (10 min from
now): Claude's ``claudeAiOauth.expiresAt``; for Codex the ``exp`` claim of the access-token
JWT. It then takes the short claim lease and runs the consumer's own refresh step
(``Consumer._refresh_claimed_credential``) in the cron container, where the real
credential lives. It asserts what AG-65 promises: the CLI refreshed, the rotation was
persisted to the same row, the row is no longer refresh-imminent, and the lease is free.

The selection by ``resolve()`` is not exercised here — the pool may prefer an API key —
it is covered against real MySQL by ``tests/integration/test_refresh_lease.py``.

**This ROTATES a real single-use refresh token.** It runs against the deployment DB so
the new token lands in the row the credential came from; every OTHER copy of that token
(another stack, a developer login it was imported from) stops working. Use credentials
registered only in this stack. A rotation is never undone. Only a run in which the CLI
did NOT rotate restores the original payload (the stored expiry was the only change).

Gated like the other stack e2e tests: ``AGENTO_E2E=1 bin/test``. Needs the stack under test
(``AGENTO_E2E_PROJECT_ROOT``, default this checkout's dev stack) up with a running ``cron``.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from agento.framework.cli._project import compose_file_flags
from tests.e2e._stack import PROJECT_ROOT as _PROJECT_ROOT

_COMPOSE = ["docker", "compose", *compose_file_flags(_PROJECT_ROOT)]
_E2E_ENABLED = os.environ.get("AGENTO_E2E") == "1"


def _cron_running() -> bool:
    res = subprocess.run(
        [*_COMPOSE, "ps", "--status", "running", "--services"],
        capture_output=True, text=True, timeout=30,
    )
    return res.returncode == 0 and "cron" in res.stdout.split()


pytestmark = [
    pytest.mark.e2e,
    # A rotation must not race a run that uses the same token: run alone, after the rest
    # (`pytest -m rotates_credentials -n 0`).
    pytest.mark.rotates_credentials,
    pytest.mark.skipif(
        not _E2E_ENABLED,
        reason="set AGENTO_E2E=1 to run (rotates a real OAuth refresh token)",
    ),
    pytest.mark.skipif(
        _E2E_ENABLED and not _cron_running(),
        reason="the dev stack's cron container is not running",
    ),
]

# Runs inside the cron container as uid `agent`, the consumer's own user. It prints one
# `RESULT <json>` line with facts only — never a token (SEC-6).
_SCRIPT = r'''
import base64, json, logging, os, time
from types import SimpleNamespace

from agento.framework.agent_manager.credential_resolver import CredentialResolver
from agento.framework.agent_manager.credential_store import (
    get_credential, release_credential_lease, update_refreshed_credentials,
)
from agento.framework.bootstrap import bootstrap
from agento.framework.consumer import _CLAIM_LEASE_TTL_SECONDS, Consumer
from agento.framework.consumer_config import ConsumerConfig
from agento.framework.database_config import DatabaseConfig
from agento.framework.db import get_connection
from agento.framework.harness.registry import workspace_adapter_for

SCOPE = os.environ["E2E_SCOPE"]
HARNESS = SCOPE  # the claude/codex harness owns the scope of the same name
OWNER = f"e2e-ag65-{SCOPE}-{os.getpid()}"
NEAR = int(time.time()) + 600  # inside the 2100 s refresh horizon


def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def near_expiry(creds):
    creds = json.loads(json.dumps(creds))
    if SCOPE == "claude":
        creds["raw_auth"]["credentials"]["claudeAiOauth"]["expiresAt"] = NEAR * 1000
    else:
        tokens = creds["raw_auth"]["tokens"]
        head, payload, sig = tokens["access_token"].split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        claims["exp"] = NEAR
        tokens["access_token"] = f"{head}.{b64(claims)}.{sig}"
        creds["subscription_key"] = tokens["access_token"]
    return creds


def tokens_of(creds):
    raw = creds.get("raw_auth") or {}
    if SCOPE == "claude":
        o = raw["credentials"]["claudeAiOauth"]
        return o.get("accessToken"), o.get("refreshToken")
    t = raw["tokens"]
    return t.get("access_token"), t.get("refresh_token")


db = DatabaseConfig.from_env()
conn = get_connection(db)
bootstrap(db_conn=conn, quiet=True)
out = {"scope": SCOPE}
with conn.cursor() as cur:
    cur.execute(
        "SELECT id, label FROM credential WHERE scope = %s AND type = 'oauth' AND enabled = 1 "
        "AND status = 'ok' AND (throttled_until IS NULL OR throttled_until < UTC_TIMESTAMP()) "
        "AND (leased_until IS NULL OR leased_until < UTC_TIMESTAMP()) ORDER BY id LIMIT 1",
        (SCOPE,),
    )
    row = cur.fetchone()
if row is None:
    print("RESULT " + json.dumps({**out, "skip": "no healthy oauth credential"}))
    raise SystemExit(0)
cred_id = row["id"]
out["credential"] = f"id={cred_id} label={row['label']}"
original = get_credential(conn, cred_id).credentials
old_access, old_refresh = tokens_of(original)

update_refreshed_credentials(conn, cred_id, near_expiry(original))
conn.commit()
resolver = CredentialResolver(lease_ttl_seconds=_CLAIM_LEASE_TTL_SECONDS)
out["imminent_before"] = resolver.refresh_imminent(get_credential(conn, cred_id))

# The claim lease, exactly as select_credential takes it in its selecting UPDATE.
with conn.cursor() as cur:
    cur.execute(
        "UPDATE credential SET used_at = UTC_TIMESTAMP(6), lease_owner = %s, "
        "leased_until = UTC_TIMESTAMP() + INTERVAL %s SECOND WHERE id = %s",
        (OWNER, _CLAIM_LEASE_TTL_SECONDS, cred_id),
    )
conn.commit()
claimed = get_credential(conn, cred_id)

consumer = Consumer(db, ConsumerConfig(), logging.getLogger("e2e-ag65"))
job = SimpleNamespace(id=0, attempt=1)
started = time.monotonic()
try:
    _, leased = consumer._refresh_claimed_credential(job, HARNESS, SCOPE, claimed, OWNER)
    out["kept_lease"] = leased
except Exception as exc:  # reported, then asserted on by the host side
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
out["seconds"] = round(time.monotonic() - started, 1)

conn.commit()
after = get_credential(conn, cred_id)
new_access, new_refresh = tokens_of(after.credentials)
out["access_changed"] = new_access != old_access and new_access != tokens_of(near_expiry(original))[0]
out["refresh_changed"] = new_refresh != old_refresh
out["status"] = str(after.status.value if hasattr(after.status, "value") else after.status)
out["ttl_after"] = workspace_adapter_for(HARNESS).credential_ttl_seconds(after)
out["imminent_after"] = resolver.refresh_imminent(after)
out["lease_free"] = after.lease_owner is None

if not out["access_changed"] and not out["refresh_changed"]:
    # No rotation: the stored expiry was the only change, so put the original back.
    update_refreshed_credentials(conn, cred_id, original)
    out["restored"] = True
release_credential_lease(conn, cred_id, OWNER)  # no-op when the refresh released it
conn.commit()
print("RESULT " + json.dumps(out))
'''


def _refresh_in_cron(scope: str) -> dict:
    res = subprocess.run(
        [*_COMPOSE, "exec", "-T", "--user", "agent", "-e", f"E2E_SCOPE={scope}",
         "cron", "python", "-"],
        input=_SCRIPT, capture_output=True, text=True, timeout=300,
    )
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("RESULT ")]
    assert lines, f"no result (rc={res.returncode}):\n{res.stdout[-2000:]}\n{res.stderr[-2000:]}"
    return json.loads(lines[-1].removeprefix("RESULT "))


@pytest.mark.parametrize("scope", ["claude", "codex"])
def test_a_near_expiry_oauth_credential_is_refreshed_out_of_band(scope):
    result = _refresh_in_cron(scope)
    if "skip" in result:
        pytest.skip(f"{scope}: {result['skip']}")
    summary = json.dumps(result, indent=2)

    assert result["imminent_before"] is True, summary
    assert "error" not in result, summary
    assert result["access_changed"] is True, f"the CLI did not refresh:\n{summary}"
    assert result["kept_lease"] is False, summary
    assert result["lease_free"] is True, summary
    assert result["imminent_after"] is False, summary
    assert result["status"] == "ok", summary
