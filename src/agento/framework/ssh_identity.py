"""Per-run SSH identity — the private key is never written to a file.

`workspace:build` used to decrypt ``agent_view/identity/ssh_private_key`` into
``<build_dir>/.ssh/id_rsa`` (mode 0600). Every agent_view runs in ONE container, as ONE
uid, on ONE shared ``/workspace``, so 0600/owner=agent means "readable by every view" —
a PROD agent read a peer view's key off the disk and pushed with it.

So: only the **non-secret** parts (``id_rsa.pub``, ``config``, ``known_hosts``) are ever
materialized as files. The private key travels config -> process memory -> env -> an
inherited file descriptor -> ``ssh-add`` (see ``ssh_prelude.py``), and the agent is handed
an ``SSH_AUTH_SOCK`` — a *use* capability, never the credential.

This is NOT isolation between agent_views: under one uid the environ, the descriptor and
the agent socket are all readable by a same-uid peer for as long as they exist. See
``DECISIONS.md`` for the dated waiver and the residual channels it covers.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SSH_PRIVATE_KEY_PATH = "agent_view/identity/ssh_private_key"
SSH_PUBLIC_KEY_PATH = "agent_view/identity/ssh_public_key"
SSH_CONFIG_PATH = "agent_view/identity/ssh_config"
SSH_KNOWN_HOSTS_PATH = "agent_view/identity/ssh_known_hosts"

# Input sanity bound on a secret that travels through the process environment — NOT the
# mechanism behind "never a file" (that is the process substitution in ssh_prelude.py,
# which has no size threshold). 16 KiB is 4x the largest key anyone uses (RSA-8192 PEM
# ~= 6.4 KiB, ed25519 ~= 0.4 KiB). Mirrored as `maxLength` on the config field.
MAX_SSH_PRIVATE_KEY_BYTES = 16384

# `ssh-agent -t` for a local `agento run` — BOTH its forms, headless (`--prompt`) and
# interactive: an operator-driven run is human-paced and has no job timeout to borrow.
# The consumer passes its configured job timeout instead.
SSH_TTL_LOCAL_RUN = 43200

# Bounded prefix the key scanner reads from every regular file, whatever its total size.
# DERIVED from the configured maximum, never an independent literal: detection requires the
# END marker inside the window, so a window smaller than the largest key this system accepts
# would silently miss a renamed copy of one it wrote itself. The margin covers the envelope
# (BEGIN/END lines, RFC-1421 headers, CRLF) plus leading whitespace.
_SCAN_PREFIX_BYTES = MAX_SSH_PRIVATE_KEY_BYTES + 2048

# An ANCHORED, structurally complete PEM envelope. A substring match would delete source
# code: this repo (and any repo an agent clones into workspace/artifacts) carries quoted
# PEM headers inside test fixtures, and `prune_stale_build_keys` deletes without asking.
_PEM_BEGIN = re.compile(rb"-----BEGIN ([A-Z0-9 ]*)PRIVATE KEY-----[ \t]*\r?\n")
# The line after BEGIN must be base64 body or RFC-1421 header material.
_PEM_SECOND_LINE = re.compile(rb"^(?:[A-Za-z0-9+/=]+|[A-Za-z][A-Za-z0-9-]*:.*)$")


@dataclass(frozen=True)
class ResolvedSshIdentity:
    """The four identity values, resolved once per run.

    ``private_key`` is ``repr=False`` so the plaintext cannot reach a log line, a
    traceback, a debugger frame or a pytest assertion diff through the default
    dataclass ``repr`` — the same protection ``HarnessRunContext.credential`` carries.
    """

    private_key: str = field(default="", repr=False)
    public_key: str = ""
    config: str = ""
    known_hosts: str = ""


def resolve_ssh_identity(agent_config_svc=None) -> ResolvedSshIdentity:
    """Read the four config paths ONCE (``obscure`` values decrypt on ``.get()``).

    The single place that touches config: ``materialize_ssh_public_identity`` and
    ``ssh_run_env`` both take the resolved value, so the public files and the private key
    can never come from two different config snapshots (a rotation landing between two
    reads would pair the old ``id_rsa.pub`` with the new key).

    Raises ``ValueError`` when the configured key is over ``MAX_SSH_PRIVATE_KEY_BYTES`` —
    fail closed, rather than pushing an unbounded secret through the environment.
    """
    if agent_config_svc is None:
        return ResolvedSshIdentity()

    private = agent_config_svc.get(SSH_PRIVATE_KEY_PATH) or ""
    public = agent_config_svc.get(SSH_PUBLIC_KEY_PATH) or ""
    config = agent_config_svc.get(SSH_CONFIG_PATH) or ""
    known_hosts = agent_config_svc.get(SSH_KNOWN_HOSTS_PATH) or ""

    size = len(private.encode("utf-8"))
    if size > MAX_SSH_PRIVATE_KEY_BYTES:
        raise ValueError(
            f"{SSH_PRIVATE_KEY_PATH} is {size} bytes, over the "
            f"{MAX_SSH_PRIVATE_KEY_BYTES}-byte limit"
        )

    if private:
        _warn_if_unusable(private)

    return ResolvedSshIdentity(
        private_key=private,
        public_key=public,
        config=config,
        known_hosts=known_hosts,
    )


def _warn_if_unusable(private: str) -> None:
    """Name the reason a key will fail, before ``ssh-add`` only says it did.

    Parse, do not eyeball. A structural check on the BEGIN/END envelope and a plausible
    length lets through a key that is the right shape and the wrong bytes — a paste that
    lost a middle line, a CRLF-mangled body — and still yields "Permission denied
    (publickey)". This is the same call ``identity:check`` makes, so the run's log and
    the CLI agree by construction.

    Last line of defence: ``config:set`` and the admin TUI both reject an unparsable key,
    but a row written before that guard existed (or by a direct DB edit) still reaches
    here. Never fatal — the run continues and the prelude reports the missing identity.
    """
    from .ssh_keys import EncryptedKeyError, derive_public_key

    try:
        derive_public_key(private)
    except EncryptedKeyError:
        logger.warning(
            "resolve_ssh_identity: %s is passphrase-protected; the agent cannot use it "
            "and git over SSH will fail. Store an unencrypted key.",
            SSH_PRIVATE_KEY_PATH,
        )
    except ValueError:
        # Byte count only — never the key, and never the exception text, which some
        # backends echo key material into.
        logger.warning(
            "resolve_ssh_identity: %s does not parse as an SSH private key (%d bytes); "
            "git over SSH will fail. Run `agento agent_view:identity:check <code>`.",
            SSH_PRIVATE_KEY_PATH, len(private),
        )


def materialize_ssh_public_identity(
    home_dir: Path | str,
    resolved: ResolvedSshIdentity,
) -> None:
    """Write ONLY the non-secret SSH files into ``home_dir/.ssh``.

    ``id_rsa`` is never written — by anything, anywhere. ``ssh``/``git`` genuinely need
    these three as files, and none of them is a credential.
    """
    if not (resolved.public_key or resolved.config or resolved.known_hosts):
        return

    ssh_dir = Path(home_dir) / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(ssh_dir, 0o700)

    for name, value in (
        ("id_rsa.pub", resolved.public_key),
        ("config", resolved.config),
        ("known_hosts", resolved.known_hosts),
    ):
        if not value:
            continue
        target = ssh_dir / name
        target.write_text(value)
        os.chmod(target, 0o600)


def scrub_ssh_private_key(home_dir: Path | str) -> None:
    """Remove a LEGACY ``home_dir/.ssh/id_rsa`` — and fail closed if it survives.

    A build made before this change can still carry a key that the run-dir copy brought
    along. This is a security PRECONDITION of the run, not cleanup: a key we could not
    remove is a key the agent can read, which is the whole bug. So it logs ERROR and
    raises rather than best-effort warning.
    """
    target = Path(home_dir) / ".ssh" / "id_rsa"
    try:
        target.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.error(
            "SECURITY: could not remove legacy SSH private key %s (%s) — refusing to "
            "start a run that could read it",
            target, exc,
        )
        raise
    try:
        target.lstat()
    except FileNotFoundError:
        return
    logger.error(
        "SECURITY: legacy SSH private key %s still exists after unlink — refusing to "
        "start a run that could read it",
        target,
    )
    raise RuntimeError(f"could not remove legacy SSH private key {target}")


def _looks_like_private_key(prefix: bytes) -> bool:
    """True when ``prefix`` STARTS a complete PEM private-key envelope.

    Anchored at byte 0 (after leading whitespace only) and requiring a matching END, so
    a source file that merely quotes a PEM header is not a hit. Documented scope: this
    finds keys stored the way this system stored them. A key embedded mid-file,
    re-encoded, compressed or split is out of scope — the callers say so in their output.
    """
    body = prefix.lstrip()
    match = _PEM_BEGIN.match(body)
    if match is None:
        return False
    kind = match.group(1)
    rest = body[match.end():]
    first_line = rest.split(b"\n", 1)[0].strip()
    if not first_line or not _PEM_SECOND_LINE.match(first_line):
        return False
    return b"-----END " + kind + b"PRIVATE KEY-----" in rest


class SshKeyPurgeError(RuntimeError):
    """A key may still be on disk: the scan could not read everything, or an unlink failed.

    Fail closed. A scanner that skips what it cannot read reports "clean" for the one
    tree an attacker made unreadable, and a prune that logs an unlink failure and returns
    the remaining keys as "removed" reports success for the exact exposure it exists to
    end. Both are indistinguishable from a real clean sweep at the call site — so neither
    is allowed to return normally.
    """


@dataclass(frozen=True)
class SshKeyScan:
    """Result of one scan: what was found, and what could not be looked at.

    ``unreadable`` is never empty-and-ignored: a caller that does not act on it has not
    established the absence of a key, only the absence of a key it could see.
    """

    keys: tuple[Path, ...] = ()
    unreadable: tuple[str, ...] = ()


def find_private_keys(root: Path | str) -> SshKeyScan:
    """Every private key under ``root``: a complete PEM envelope, or the name ``id_rsa``.

    The ONE scanner, shared by ``prune_stale_build_keys`` and ``workspace:ssh-purge`` so
    the two can never disagree about what a key is. Never follows symlinks, and reads a
    bounded prefix of every regular file **regardless of that file's total size** (a
    padded or bundled PEM must not survive a purge by being large).

    A path that cannot be listed, stat-ed or read is reported in ``unreadable`` — it is
    NOT skipped silently. ``FileNotFoundError`` alone is benign: a file that vanished
    mid-walk (an agent run writing under ``workspace/artifacts``) is a file that is gone.
    """
    base = Path(root)
    hits: list[Path] = []
    unreadable: list[str] = []

    def _blocked(exc: OSError) -> None:
        if isinstance(exc, FileNotFoundError):
            return
        unreadable.append(f"{exc.filename or base}: {exc.strerror or exc}")

    for dirpath, _dirnames, filenames in os.walk(base, followlinks=False, onerror=_blocked):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if path.is_symlink():
                    continue
                if not path.is_file():
                    continue
            except OSError as exc:
                _blocked(exc)
                continue
            if name == "id_rsa":
                hits.append(path)
                continue
            try:
                with path.open("rb") as handle:
                    prefix = handle.read(_SCAN_PREFIX_BYTES)
            except OSError as exc:
                _blocked(exc)
                continue
            if _looks_like_private_key(prefix):
                hits.append(path)
    return SshKeyScan(keys=tuple(sorted(hits)), unreadable=tuple(sorted(unreadable)))


def prune_stale_build_keys(view_builds_dir: Path | str) -> list[Path]:
    """Remove every private key under one agent_view's ``builds/`` tree; return what went.

    Pre-fix builds still on disk at upgrade time keep a key in EVERY retained generation.
    Removal is by content, not by the name ``.ssh/id_rsa``, so AC1's "no private key
    anywhere in that view's build tree" is what the code actually enforces.

    Raises ``SshKeyPurgeError`` when anything under the tree could not be scanned or a
    key could not be unlinked: the build must not proceed while a readable key may still
    sit where the PROD attack found one. Every removable key is removed first, so the
    admin's follow-up (``workspace:ssh-purge``) faces the smallest possible remainder.
    """
    base = Path(view_builds_dir)
    if not base.is_dir():
        return []
    scan = find_private_keys(base)
    removed: list[Path] = []
    failed: list[str] = []
    for path in scan.keys:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            failed.append(f"{path}: {exc.strerror or exc}")
            continue
        removed.append(path)
    problems = [*scan.unreadable, *failed]
    if problems:
        raise SshKeyPurgeError(
            f"legacy SSH private keys may still be readable under {base} — "
            f"removed {len(removed)}, unresolved: " + "; ".join(problems) +
            ". Fix the permissions and run `bin/agento workspace:ssh-purge`."
        )
    return removed


# Env names this run OWNS. A value one of these carries must come from THIS run's
# resolved identity — never from whatever the spawning process inherited (a
# docker-compose `environment:` entry, the consumer's own environ, a peer's exported
# socket). An identity-less run contributes NOTHING to the env (`ssh_run_env` returns
# `{}`), so a merge alone cannot overwrite an inherited value: it has to be removed.
RUN_OWNED_SSH_ENV_VARS = frozenset({
    "AGENTO_SSH_PRIVATE_KEY",
    "AGENTO_SSH_TTL",
    "GIT_SSH_COMMAND",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
})


def without_run_owned_ssh_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` minus every name a run's own SSH identity is allowed to set."""
    return {k: v for k, v in env.items() if k not in RUN_OWNED_SSH_ENV_VARS}


def ssh_private_key_env(resolved: ResolvedSshIdentity) -> dict[str, str]:
    """The in-memory delivery channel — the ONLY helper that puts the key into env.

    Touches no filesystem and takes no ``home_dir``.
    """
    key = resolved.private_key
    if not key:
        return {}
    return {"AGENTO_SSH_PRIVATE_KEY": key if key.endswith("\n") else key + "\n"}


def git_ssh_command_env(
    home_dir: Path | str | None,
    *,
    has_private_key: bool,
) -> dict[str, str]:
    """``GIT_SSH_COMMAND`` pointing at the run's own non-secret SSH files.

    ``has_private_key`` comes from the caller (``ssh_private_key_env(...) != {}``): with
    no key file anywhere, a HOME path alone can no longer tell whether this run has an
    identity.
    """
    if not has_private_key or home_dir is None:
        return {}
    ssh_dir = Path(home_dir) / ".ssh"
    parts = ["ssh"]
    config = ssh_dir / "config"
    if config.is_file():
        parts += ["-F", str(config)]
    known_hosts = ssh_dir / "known_hosts"
    if known_hosts.is_file():
        parts += ["-o", f"UserKnownHostsFile={known_hosts}"]
    # The agent socket is now the run's ONLY identity, and the config field a deployment
    # already has may still say `IdentityFile ~/.ssh/id_rsa` + `IdentitiesOnly yes` (the
    # field's own label invites Host/IdentityFile directives). That pair selects a file
    # which deliberately no longer exists AND suppresses the loaded agent key, so such a
    # view would stop authenticating. A command-line `-o` wins over the config file, so
    # forcing it here keeps every pre-existing ssh_config working.
    parts += ["-o", "IdentitiesOnly=no"]
    return {"GIT_SSH_COMMAND": " ".join(parts)}


def ssh_run_env(
    resolved: ResolvedSshIdentity,
    home_dir: Path | str | None,
    *,
    ttl_seconds: int,
) -> dict[str, str]:
    """The one env entry point both spawn paths call.

    Takes the RESOLVED identity, never the config service, so it cannot perform a second
    read. ``{}`` when the view has no private key, so such a view contributes nothing.
    ``ttl_seconds`` is explicit on both paths — the wrapper's own fallback is never the
    value a real run uses.
    """
    key_env = ssh_private_key_env(resolved)
    if not key_env:
        return {}
    return {
        **key_env,
        **git_ssh_command_env(home_dir, has_private_key=True),
        "AGENTO_SSH_TTL": str(int(ttl_seconds)),
    }
