"""Runtime value objects for a harness run — context, request, result.

Secret-bearing fields are ``repr=False`` so a ``logger.debug("%r", ctx)`` cannot
leak a credential into the logs.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .descriptor import HarnessId, ProviderId


@dataclass(frozen=True)
class McpServerStatus:
    """One MCP server entry from a CLI's session-init self-report."""

    name: str
    status: str


@dataclass(frozen=True)
class McpInitReport:
    """A harness CLI's self-report of the MCP servers visible at session start.

    Generic by design — the framework knows about "MCP server init", not about any
    specific server. An empty ``servers`` tuple is a *valid* report meaning "the CLI
    started and saw no MCP servers", distinct from a missing report
    (``RunResult.mcp_init is None``) meaning the harness exposed no init signal at
    all. No ``raw`` field: raw CLI events can carry prompts, tool arguments and
    customer data; ``(name, status)`` tuples suffice for telemetry.
    """

    servers: tuple[McpServerStatus, ...]


@dataclass
class RunResult:
    """Harness-agnostic result of a single agent CLI run."""

    raw_output: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    session_id: str | None = None
    harness: str | None = None
    provider: str | None = None
    model: str | None = None
    prompt: str | None = None
    mcp_init: McpInitReport | None = None

    @property
    def stats_line(self) -> str:
        return (
            f"turns={self.num_turns or '?'} "
            f"in={self.input_tokens or '?'} "
            f"out={self.output_tokens or '?'} "
            f"cost_usd={self.cost_usd or '?'} "
            f"duration_ms={self.duration_ms or '?'}"
        )


@dataclass(frozen=True)
class RunRequest:
    """What to run. ``session_id`` set means resume that session instead of starting new."""

    prompt: str
    session_id: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class HarnessRunContext:
    """Everything a harness needs for one run, resolved by the caller.

    ``credential`` is ``None`` in exactly two cases: the provider does not require
    one, or the context is built for an explicitly interactive login flow (the
    operator will run ``/login`` inside the sandbox). ``SubprocessRunner.execute()``
    is headless and rejects the second case; ``CommandBuilder.interactive()`` accepts it.
    """

    harness: HarnessId
    provider: ProviderId
    working_dir: str = "/workspace"
    model: str | None = None
    home_dir: str | None = None
    timeout_seconds: int = 1200
    credential_required: bool = True
    credential: object | None = field(default=None, repr=False)
    extra_env: dict[str, str] = field(default_factory=dict, repr=False)
    # The declaring module's OWN config, restricted to the fields its manifest
    # allow-lists via `runtime_config_fields`. Never the whole resolved config:
    # this context builds argv, and `resolve_all()` decrypts every module's
    # `obscure` values. Populated by `get_harness_config`.
    harness_config: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ToolboxConnectionSpec:
    """How an agent should reach the Toolbox MCP endpoint.

    A pure data object: how it is materialized (a config file? CLI flags? an
    extension install? env vars?) belongs entirely to the harness's workspace
    adapter — there is no shared MCP-file writer, because not every harness has
    an MCP config file at all.
    """

    name: str
    transport: str
    url: str
    headers: dict[str, str] = field(default_factory=dict, repr=False)
