"""The credential-store environment — and why an agent process must not inherit it.

The consumer runs in the cron container, which holds the framework's own
credential-store capability in its environment: the database credentials
(``MYSQL_*``), the passphrase every stored credential is decrypted with
(``AGENTO_ENCRYPTION_KEY``, read by :mod:`agento.framework.crypto`), and any
``CONFIG__*`` override, which is a plaintext config value that may itself be a
secret.

An agent process spawned from the consumer used to inherit all of it, so the
agent could read and decrypt EVERY credential in the store — including another
agent_view's SSH private key — by running ``bin/agento`` or by connecting to the
database directly. Removing these names from the spawn environment removes that
capability from the process the model drives.

It is a reduction, not a boundary. Every agent_view runs as one uid (``agent``)
in one container, so a same-uid process can still read the cron env file
(``/opt/cron-agent/env``, mode 0644) and decrypt every stored credential from it.
``/proc/<consumer-pid>/environ`` is no longer a second route —
:mod:`agento.framework.process_hardening` makes framework processes non-dumpable —
but that changes nothing about the capability the file grants.

Status: DECISIONS.md D-SSH-1 residual channel (6), **accepted by the owner
2026-08-25 until AG-42 delivers Option B** (a per-view uid, which closes this
along with the peer-artifact reads). The env file cannot be removed on its own:
the crontab and the consumer are started by ``su - agent -c "source
/opt/cron-agent/env; …"``, so the secrets need a root-owned launcher first. See
``docs/architecture/option-b-per-view-uid.md``.
"""
from __future__ import annotations

# Exact names the framework itself puts in the container environment.
CREDENTIAL_STORE_ENV_VARS = frozenset({
    "AGENTO_ENCRYPTION_KEY",
})

# Prefixes: every name under them is a credential-store input, so the set is
# closed under a new variable being added later (a new MYSQL_* knob, a new
# CONFIG__* override) without this file having to enumerate it.
CREDENTIAL_STORE_ENV_PREFIXES = ("MYSQL_", "CONFIG__")


def without_credential_store_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` minus every name that grants access to the credential store.

    The run's OWN credential (the provider API key a harness needs) is not part
    of this: it is merged in afterwards by the runner, deliberately and per run.
    """
    return {
        k: v
        for k, v in env.items()
        if k not in CREDENTIAL_STORE_ENV_VARS
        and not k.startswith(CREDENTIAL_STORE_ENV_PREFIXES)
    }
