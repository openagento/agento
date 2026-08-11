"""Protocols a harness module implements — grouped by role, not one file per class.

A harness module ships one :class:`AgentHarnessAdapter` and registers it under
``agent_harnesses`` in its ``di.json``. The framework never imports a harness
module directly and never branches on a harness id.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .descriptor import CredentialRegistrationMode, CredentialScope
from .runtime import HarnessRunContext, RunRequest, RunResult, ToolboxConnectionSpec

# Config path prefix for agent CLI settings.
AGENT_CONFIG_PREFIX = "agent_view/"


@runtime_checkable
class Runner(Protocol):
    """Executes one agent run and returns its parsed result.

    No ``resume()`` — a resume is a ``RunRequest`` carrying a ``session_id``, which the
    harness's ``CommandBuilder`` turns into its own resume flags. ``SubprocessRunner`` is
    the shipped implementation, but a harness that talks HTTP instead of spawning a process
    satisfies this just as well, which is why consumers type against the protocol rather
    than the class.
    """

    def execute(self, request: RunRequest) -> RunResult: ...

    def observe(
        self,
        *,
        on_pid: Callable[[int], None] | None = None,
        on_session_id: Callable[[str], None] | None = None,
    ) -> None:
        """Register progress callbacks for a run that is about to start.

        A **method**, not two assignable attributes: the consumer used to set
        ``runner.pid_callback`` / ``runner.session_id_callback`` directly, which made them a
        required part of the contract without appearing in it — a structurally valid runner
        using ``__slots__`` (or a frozen dataclass) crashed with ``AttributeError``.

        Both hooks are best-effort and may be ignored: ``on_pid`` is meaningless for a
        runner that spawns no process, and a harness with no streaming session id simply
        never calls ``on_session_id``. Implementations that cannot report either may no-op.
        """
        ...


class UnsupportedRegistrationMode(Exception):
    """Raised when a mode is requested that the provider does not declare.

    A defensive contract only: the registry validates the mode against
    ``ModelProviderDescriptor.registration_modes`` before dispatching, so a correct
    declaration never reaches this.
    """


@dataclass
class AuthResult:
    """Normalised credentials extracted after a successful authentication.

    Secret-bearing fields are ``repr=False`` — this object is passed around and
    logged during registration flows.
    """

    subscription_key: str = ""
    refresh_token: str | None = None
    expires_at: int | None = None
    subscription_type: str | None = None
    id_token: str | None = None
    raw_auth: dict | None = None

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"AuthResult(subscription_type={self.subscription_type!r}, "
            f"expires_at={self.expires_at!r}, <secrets hidden>)"
        )


@runtime_checkable
class CommandBuilder(Protocol):
    """The single place a harness's CLI flags are defined.

    Both modes come from one implementation so headless and interactive can never
    drift (they did: the old ``CliInvoker.headless_command`` omitted the per-job
    ``--mcp-config``/``--strict-mcp-config`` that the runner added).
    """

    def headless(self, ctx: HarnessRunContext, req: RunRequest) -> list[str]:
        """Command for a one-shot headless run, or a resume when ``req.session_id`` is set."""
        ...

    def interactive(self, ctx: HarnessRunContext, *, yolo: bool = False) -> list[str]:
        """Command to spawn the CLI in interactive TTY mode (no prompt).

        ``yolo`` includes the harness's skip-approvals flag — the same bypass the
        headless path always uses. Accepts ``ctx.credential is None`` even for a
        credential-requiring provider: that is the ``/login`` flow.
        """
        ...


@runtime_checkable
class WorkspaceAdapter(Protocol):
    """Materializes a harness's on-disk configuration into a workspace build.

    Formerly ``ConfigWriter``. ``capture_refreshed_credentials`` is part of the
    protocol now instead of being probed with ``getattr``.
    """

    def prepare_workspace(
        self,
        working_dir: Path,
        agent_config: dict[str, str],
        *,
        agent_view_id: int | None = None,
        toolbox_url: str,
    ) -> None: ...

    def inject_runtime_params(self, artifacts_dir: Path, *, job_id: int) -> None: ...

    def owned_paths(self) -> tuple[set[str], set[str]]:
        """Return (files, dirs) owned by this harness.

        When copying a build into a per-job run dir, the framework copies these
        items (instead of symlinking) so they can be modified per job.
        """
        ...

    def persistent_home_paths(self) -> list[str]:
        """HOME-relative paths that must survive workspace rebuilds.

        Session/state artifacts (e.g. ``.claude/projects``) which the framework
        symlinks from the immutable build dir to a per-agent_view persistent
        ``state/`` directory. An empty list means no persistent home state.
        """
        ...

    def write_credentials(self, build_dir: Path, credential: object) -> None:
        """Materialize harness-specific credential files into ``build_dir``.

        ``credential.credentials`` is the decrypted payload (flat fields:
        ``subscription_key``, ``refresh_token``, ``expires_at``,
        ``subscription_type``, ``id_token``, ``raw_auth``). Each harness rewrites it
        into the format its CLI expects.
        """
        ...

    def credential_env(self, credential: object) -> dict[str, str]:
        """Env-var overrides derived from the credential payload.

        Sibling to :meth:`write_credentials` for the env delivery path: API-key
        credential types materialize as a single ``{KEY: value}`` entry; types whose
        CLIs require on-disk auth return ``{}``.
        """
        ...

    def remove_credentials(self, target_dir: Path) -> None:
        """Delete this harness's credential state from ``target_dir``, keeping config.

        Needed for the credential-free interactive path: a run dir is COPIED from the
        current build, which may already hold credentials a previous
        ``materialize_agent_credentials`` wrote. Without an explicit removal step, an
        `agento run` that deliberately proceeds without a credential (empty pool → operator
        wants `/login`) would silently inherit a disabled, errored or deregistered one — so
        the "credential-free" flow would not be credential-free.

        Must remove ONLY credential material. Model/MCP/permission config in the same files
        has to survive, or the login would start against an unconfigured agent.
        """
        ...

    def capture_refreshed_credentials(self, home: Path, credential: object, conn) -> bool:
        """Persist credentials the CLI refreshed on disk back into the DB.

        Returns ``True`` when something was written. Part of the protocol so the
        consumer no longer probes for it with ``getattr``.
        """
        ...

    def serialize_toolbox_connection(
        self, spec: ToolboxConnectionSpec, target_dir: Path
    ) -> None:
        """Materialize the Toolbox connection however this harness consumes it.

        Deliberately unconstrained: an MCP JSON file, CLI flags, an extension install,
        or env vars are all valid — the framework does not assume MCP (Pi, for one, has
        no MCP at all).

        **Not yet on the framework's own call path.** The two shipped harnesses still
        materialize their Toolbox wiring inside :meth:`prepare_workspace`, which already
        keeps the format harness-side. This method is the declared seam for the
        transport-agnostic rewrite deferred to Etap 2 (see
        ``docs/architecture/harness-contract.md``); it is implemented and tested on every
        shipped adapter so it cannot rot in the meantime.
        """
        ...

    def migrate_legacy_workspace_config(self, build_dir: Path, workspace_root: Path) -> None:
        """Best-effort migration of legacy shared-HOME config into the new build layout."""
        ...


@dataclass(frozen=True)
class ToolUse:
    """Single tool invocation observed in an agent session."""

    name: str
    tool_use_id: str


@dataclass(frozen=True)
class ParseSummary:
    """Result of parsing a session transcript.

    ``total_json_lines`` counts lines whose JSON parses (regardless of shape);
    ``recognized_records`` counts lines whose outer shape matched. Non-zero
    ``total_json_lines`` with zero ``recognized_records`` is the canonical signal
    for "the harness changed its transcript format silently".
    """

    total_json_lines: int
    recognized_records: int
    tool_uses: tuple[ToolUse, ...]


@runtime_checkable
class TranscriptReader(Protocol):
    """Reads a harness's own session transcript format."""

    def parse(self, session_id: str) -> ParseSummary:
        """Return parse stats + tool uses for ``session_id``.

        Raises:
            FileNotFoundError: when no transcript exists for the session.
        """
        ...

    def iter_tool_uses(self, session_id: str) -> Iterable[ToolUse]:
        """Yield every tool invocation recorded for ``session_id``."""
        ...


@runtime_checkable
class CredentialAuthenticator(Protocol):
    """How credentials for one credential scope are obtained.

    The protocol is **total** — both methods always exist, so structural typing
    (``isinstance`` on a ``runtime_checkable`` Protocol) actually passes. What limits
    the available modes is the declaration: the registry validates the requested mode
    against ``registration_modes`` before calling, and an unsupported branch raises
    :class:`UnsupportedRegistrationMode`.
    """

    def authenticate_interactive(
        self, tmp_home: str, logger: logging.Logger
    ) -> AuthResult: ...

    def register_from_secret(
        self, mode: CredentialRegistrationMode, secret: str
    ) -> tuple[dict, str]:
        """Return ``(credentials_dict, credential_type)`` for a pasted secret."""
        ...


@runtime_checkable
class AgentHarnessAdapter(Protocol):
    """One object per harness, wiring its behaviour together.

    ``descriptor`` is deliberately absent: the framework builds it from ``di.json``
    so it can be enumerated without importing Python.
    """

    @property
    def command_builder(self) -> CommandBuilder: ...

    @property
    def workspace_adapter(self) -> WorkspaceAdapter: ...

    def create_runner(self, ctx: HarnessRunContext, **kwargs) -> Runner:
        """Build a runner bound to ``ctx``."""
        ...

    @property
    def transcript_reader(self) -> TranscriptReader | None: ...

    @property
    def authenticators(self) -> Mapping[CredentialScope, CredentialAuthenticator]:
        """Authenticator per credential scope.

        Keys must equal exactly the harness's credential-requiring scopes; the
        registry checks that at registration time. A harness with no
        credential-requiring provider returns an empty mapping.
        """
        ...


def get_agent_config(svc) -> dict[str, str]:
    """Extract ``agent_view/*`` config into a flat dict via the single config service.

    Filters the fully-resolved effective config (``svc.resolve_all`` — ENV -> DB ->
    config.json, incl. ENV-only fields with no DB row and decryption) to the
    ``agent_view/*`` keys. Returns ``{relative_path: value}``, e.g. ``{"model": "opus-4"}``.
    """
    return {
        path[len(AGENT_CONFIG_PREFIX):]: value
        for path, value in svc.resolve_all().items()
        if path.startswith(AGENT_CONFIG_PREFIX)
    }
