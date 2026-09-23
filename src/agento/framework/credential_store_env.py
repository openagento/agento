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

This module also names the boundary itself: the cron container's entrypoint
partitions its environment by exactly this predicate. The store shapes go to
``/opt/cron-agent/env`` (``root:root 0600``), which reaches a framework process
only through ``docker/cron/drop.py``, which reads it as root and drops privilege before
loading it (never across an ``execve``); everything else goes to
``/opt/cron-agent/env.public``, which the launcher imports into the environment.
Nothing readable at uid ``agent`` grants the store. See
``docs/architecture/cron-privileges.md``.

Status: DECISIONS.md D-SSH-1 residual channel (6) — **closed 2026-09-23**.
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


def is_credential_store_name(name: str) -> bool:
    """True if this environment name grants access to the credential store."""
    return (
        name in CREDENTIAL_STORE_ENV_VARS
        or name.startswith(CREDENTIAL_STORE_ENV_PREFIXES)
    )


def without_credential_store_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` minus every name that grants access to the credential store.

    The run's OWN credential (the provider API key a harness needs) is not part
    of this: it is merged in afterwards by the runner, deliberately and per run.
    """
    return {k: v for k, v in env.items() if not is_credential_store_name(k)}
