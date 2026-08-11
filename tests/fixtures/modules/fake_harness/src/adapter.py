"""A third harness that exists only in tests.

Its whole point is to prove the registry is genuinely open: nothing under
``src/agento/framework/`` knows the string ``"fake"``, yet registering this module's
``di.json`` gives the framework a working harness with

- TWO providers on one harness — ``fake_local`` needs no credential at all, while
  ``fake_cloud`` does — which the old single ``AgentProvider`` axis could not express;
- its own ``sandbox_package``, so the rendered sandbox Dockerfile picks it up;
- no transcript reader, exercising the ``transcript_reader is None`` branch.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from agento.framework.harness import (
    AuthResult,
    CredentialRegistrationMode,
    CredentialScope,
    HarnessRunContext,
    RunRequest,
    RunResult,
    ToolboxConnectionSpec,
    UnsupportedRegistrationMode,
)

CLOUD_SCOPE = CredentialScope("fake_cloud")


class FakeCommandBuilder:
    def headless(self, ctx: HarnessRunContext, req: RunRequest) -> list[str]:
        cmd = ["fake", "run", req.prompt or ""]
        model = req.model or ctx.model
        if model:
            cmd += ["--model", model]
        return cmd

    def interactive(self, ctx: HarnessRunContext, *, yolo: bool = False) -> list[str]:
        cmd = ["fake", "shell"]
        if yolo:
            cmd.append("--unsafe")
        if ctx.model:
            cmd += ["--model", ctx.model]
        return cmd


class FakeWorkspaceAdapter:
    def prepare_workspace(
        self,
        working_dir: Path,
        agent_config: dict[str, str],
        *,
        agent_view_id: int | None = None,
        toolbox_url: str,
    ) -> None:
        working_dir.mkdir(parents=True, exist_ok=True)
        (working_dir / "fake.json").write_text(
            json.dumps({"agent_view_id": agent_view_id, "toolbox": toolbox_url})
        )

    def inject_runtime_params(self, artifacts_dir: Path, *, job_id: int) -> None:
        (artifacts_dir / "fake-job").write_text(str(job_id))

    def owned_paths(self) -> tuple[set[str], set[str]]:
        return {"fake.json"}, set()

    def persistent_home_paths(self) -> list[str]:
        return []

    def write_credentials(self, build_dir: Path, credential: object) -> None:
        payload = getattr(credential, "credentials", None) or {}
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "fake-auth.json").write_text(json.dumps(payload))

    def credential_env(self, credential: object) -> dict[str, str]:
        payload = getattr(credential, "credentials", None) or {}
        api_key = payload.get("api_key")
        return {"FAKE_API_KEY": api_key} if api_key else {}

    def remove_credentials(self, target_dir: Path) -> None:
        (target_dir / "fake-auth.json").unlink(missing_ok=True)

    def capture_refreshed_credentials(self, home: Path, credential: object, conn) -> bool:
        return False  # this harness has nothing the CLI rotates

    def serialize_toolbox_connection(
        self, spec: ToolboxConnectionSpec, target_dir: Path
    ) -> None:
        # Deliberately NOT an .mcp.json — the framework must not assume MCP.
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "fake-toolbox.txt").write_text(f"{spec.name} {spec.transport} {spec.url}")

    def migrate_legacy_workspace_config(self, build_dir: Path, workspace_root: Path) -> None:
        return None


class FakeCloudAuthenticator:
    def authenticate_interactive(self, tmp_home: str, logger) -> AuthResult:
        raise UnsupportedRegistrationMode(
            "fake_cloud has no interactive OAuth flow; use --with-api-key"
        )

    def register_from_secret(
        self, mode: CredentialRegistrationMode, secret: str
    ) -> tuple[dict, str]:
        if mode is CredentialRegistrationMode.API_KEY:
            return {"api_key": secret}, "fake_api_key"
        if mode is CredentialRegistrationMode.ACCESS_TOKEN:
            return {"access_token": secret}, "fake_access_token"
        raise UnsupportedRegistrationMode(f"fake_cloud does not support {mode.value}")


class FakeRunner:
    """Satisfies the ``Runner`` protocol without spawning anything.

    Deliberately NOT a ``SubprocessRunner`` subclass: a harness that talks HTTP instead
    of forking a CLI must be able to satisfy the contract, and the framework must type
    against the protocol rather than the shipped base class.
    """

    def __init__(self, context: HarnessRunContext, command_builder, **kwargs) -> None:
        self.context = context
        self.command_builder = command_builder
        self.kwargs = kwargs
        self.calls: list[RunRequest] = []
        self._on_session_id = None

    def observe(self, *, on_pid=None, on_session_id=None) -> None:
        # This harness spawns no process, so on_pid is meaningless; it reports the
        # session id it invents so the consumer's resume path still works.
        self._on_session_id = on_session_id

    def execute(self, request: RunRequest) -> RunResult:
        self.calls.append(request)
        cmd = (
            self.command_builder.headless(self.context, request)
            if not self.kwargs.get("interactive")
            else self.command_builder.interactive(self.context)
        )
        session_id = request.session_id or "fake-session-1"
        if self._on_session_id is not None:
            self._on_session_id(session_id)
        return RunResult(
            raw_output=f"fake ran: {' '.join(cmd)}",
            input_tokens=11,
            output_tokens=7,
            duration_ms=5,
            session_id=session_id,
            harness=str(self.context.harness),
            provider=str(self.context.provider),
            model=request.model or self.context.model,
            prompt=request.prompt,
        )


class FakeHarnessAdapter:
    def __init__(self) -> None:
        self._command_builder = FakeCommandBuilder()
        self._workspace_adapter = FakeWorkspaceAdapter()
        self._authenticators = {CLOUD_SCOPE: FakeCloudAuthenticator()}

    @property
    def command_builder(self) -> FakeCommandBuilder:
        return self._command_builder

    @property
    def workspace_adapter(self) -> FakeWorkspaceAdapter:
        return self._workspace_adapter

    @property
    def transcript_reader(self) -> None:
        # This harness keeps no transcripts — capabilities.transcripts is false.
        return None

    @property
    def authenticators(self) -> Mapping[CredentialScope, object]:
        return self._authenticators

    def create_runner(self, ctx: HarnessRunContext, **kwargs) -> FakeRunner:
        return FakeRunner(context=ctx, command_builder=self._command_builder, **kwargs)
