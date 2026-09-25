"""Auth context v1 — the Python twin of ``src/agento/toolbox/auth-context.js``.

The toolbox (Node) verifies capabilities; Python issues them. Both apply the same pure
derivation so Python can never mint a row Node would reject, and both are held to one
fixture, ``tests/fixtures/auth_context_v1.json``. Every rule is fail-closed: a claim that
cannot be derived is ``None``, never a permissive default.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

LEGACY_REST_SUBJECT = "service:legacy-internal-rest"

TTL_CEILINGS = {"session_max_ttl": 86400, "launch_max_ttl": 43200, "capability_ttl": 300}
TTL_DEFAULTS = {"session_max_ttl": 43200, "launch_max_ttl": 3600, "capability_ttl": 30}

ENDPOINT_TRANSPORT = {
    "sse": "sse",
    "messages": "sse",
    "mcp": "http",
    "invoke": "http",
    "api": "http",
    "config_test": "http",
    "health": "http",
}
ENDPOINTS = tuple(ENDPOINT_TRANSPORT)

_MCP_ENDPOINTS = ("sse", "messages", "mcp", "invoke")

_KINDS = {
    "mcp_job": {"actor": "agent", "endpoints": _MCP_ENDPOINTS},
    "mcp_interactive": {"actor": "agent", "endpoints": _MCP_ENDPOINTS},
    "internal_rest": {"actor": "service", "endpoints": ("api", "config_test", "health")},
    "user_session": {"actor": "user", "endpoints": ("invoke",), "source": "session",
                     "ttl": "session_max_ttl"},
    "miniapp": {"actor": "user", "endpoints": ("invoke",), "source": "launch",
                "ttl": "launch_max_ttl"},
}

SINGLE_USE_KINDS = ("user_session", "miniapp")

_TRANSPORTS = ("http", "sse")
_DECIMAL_ID = re.compile(r"^[1-9][0-9]*$")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_positive_int(v) -> bool:
    return _is_int(v) and v > 0


def _is_text(v) -> bool:
    return isinstance(v, str) and len(v) > 0


def _is_version_id(v) -> bool:
    return _is_text(v) and len(v) <= 64


def _is_decimal_id(v) -> bool:
    return isinstance(v, str) and _DECIMAL_ID.match(v) is not None


def _is_string_list(v) -> bool:
    return isinstance(v, list) and all(_is_text(x) for x in v)


def _lifetime_within(created_at, expires_at, cap) -> bool:
    if not _is_int(created_at) or not _is_int(expires_at) or not _is_positive_int(cap):
        return False
    lifetime = expires_at - created_at
    return 0 < lifetime <= cap


def _transports_valid(value) -> bool:
    return (
        isinstance(value, list) and len(value) > 0
        and all(isinstance(t, str) and t in _TRANSPORTS for t in value)
        and len(set(value)) == len(value)
    )


def _subject_of(user_id):
    if _is_positive_int(user_id):
        return str(user_id)
    return user_id if _is_text(user_id) else None


def _check_source(row, source, profile, ttl_caps) -> bool:
    if not isinstance(source, dict):
        return False
    if row.get("source_kind") != profile["source"] or not _is_text(row.get("source_id")):
        return False
    if source.get("kind") != profile["source"] or _subject_of(source.get("id")) != row["source_id"]:
        return False
    subject = _subject_of(source.get("user_id"))
    if subject is None or row.get("subject_id") != subject:
        return False
    if source.get("workspace_id") != row.get("workspace_id"):
        return False
    if source.get("agent_view_id") != row.get("agent_view_id"):
        return False
    if not _is_string_list(source.get("permitted_tools")):
        return False
    caps = ttl_caps or {}
    if not _lifetime_within(source.get("created_at"), source.get("expires_at"), caps.get(profile["ttl"])):
        return False
    if not _lifetime_within(row.get("created_at"), row.get("expires_at"), caps.get("capability_ttl")):
        return False
    # The capability never outlives the session or launch it was minted from.
    return row["expires_at"] <= source["expires_at"]


def derive_auth_context(*, row, agent_view_workspace_id=None, source=None, endpoint, ttl_caps=None):
    if not isinstance(row, dict):
        return None
    profile = _KINDS.get(row.get("kind"))
    if profile is None or endpoint not in ENDPOINT_TRANSPORT:
        return None

    transports = row.get("allowed_transports")
    if not isinstance(transports, list) or not _transports_valid(transports):
        return None
    if ENDPOINT_TRANSPORT[endpoint] not in transports:
        return None

    agent_view_id = row.get("agent_view_id")
    workspace_id = row.get("workspace_id")
    job_id = row.get("job_id")
    execution_id = row.get("execution_id")
    tool_ceiling = row.get("tool_ceiling")
    viewless = agent_view_id is None
    endpoints = ("config_test",) if row["kind"] == "internal_rest" and viewless else profile["endpoints"]
    if endpoint not in endpoints:
        return None

    if row.get("actor") != profile["actor"]:
        return None
    # Nothing verifies delegation yet, so no profile may claim it.
    if row.get("on_behalf_of") is not None:
        return None

    if not viewless and not _is_positive_int(agent_view_id):
        return None
    if workspace_id is not None and not _is_positive_int(workspace_id):
        return None
    if not viewless and (workspace_id is None or workspace_id != agent_view_workspace_id):
        return None
    if job_id is not None and not _is_decimal_id(job_id):
        return None
    if execution_id is not None and not _is_text(execution_id):
        return None
    if not _is_int(row.get("expires_at")):
        return None

    app_fields = (row.get("app_artifact_code"), row.get("app_version_id"), row.get("app_launch_id"))
    has_app = any(v is not None for v in app_fields)
    has_source = row.get("source_kind") is not None or row.get("source_id") is not None
    agent_only_nulls = not has_app and tool_ceiling is None and not has_source
    subject_id = row.get("subject_id")
    permitted_tools = None
    kind = row["kind"]

    if kind == "mcp_job":
        if viewless or not _is_decimal_id(job_id) or not agent_only_nulls:
            return None
        if subject_id != str(agent_view_id):
            return None
    elif kind == "mcp_interactive":
        if viewless or job_id is not None or execution_id is not None or not agent_only_nulls:
            return None
        if subject_id != str(agent_view_id):
            return None
    elif kind == "internal_rest":
        if not agent_only_nulls or execution_id is not None:
            return None
        if not isinstance(subject_id, str) or not subject_id.startswith("service:") or len(subject_id) <= 8:
            return None
        if viewless and (workspace_id is not None or job_id is not None):
            return None
    else:
        if not _is_positive_int(workspace_id):
            return None
        if job_id is not None or execution_id is not None:
            return None
        if kind == "user_session":
            if has_app or tool_ceiling is not None:
                return None
        else:
            code, version, launch = app_fields
            if not _is_text(code) or not _is_version_id(version) or not _is_text(launch):
                return None
            if not _is_string_list(tool_ceiling):
                return None
            src = source if isinstance(source, dict) else {}
            if (src.get("launch_id") != launch or src.get("artifact_code") != code
                    or src.get("version_id") != version):
                return None
        if not _check_source(row, source, profile, ttl_caps):
            return None
        permitted_tools = list(source["permitted_tools"])

    context = {
        "actor": profile["actor"],
        "subject_id": subject_id,
        "on_behalf_of": None,
        "agent_view_id": None if viewless else agent_view_id,
        "workspace_id": workspace_id,
        "job_id": job_id,
        "execution_id": execution_id,
        "app": (
            {"artifact_code": app_fields[0], "version_id": app_fields[1], "launch_id": app_fields[2]}
            if has_app else None
        ),
        "tool_ceiling": None if tool_ceiling is None else list(tool_ceiling),
        "allowed_transports": list(transports),
        "kind": kind,
        "expires_at": row["expires_at"],
        "capability_id": None if row.get("id") is None else str(row["id"]),
    }
    return {"context": context, "single_use": kind in SINGLE_USE_KINDS, "permitted_tools": permitted_tools}


AUTH_TTL_KEYS = tuple(TTL_DEFAULTS)
_POSITIVE_DECIMAL = re.compile(r"^[1-9][0-9]*$")
_clamp_warned: set[str] = set()  # at most one entry per AUTH_TTL_KEYS name


class AuthConfigError(ValueError):
    """A ``core/auth/*`` bound could not be read or is not a positive integer."""


def auth_ttl_path(key: str) -> str:
    return f"core/auth/{key}"


def auth_ttl_env_key(key: str) -> str:
    return f"CONFIG__CORE__AUTH__{key.upper()}"


def compute_auth_ttls(*, env, default_overrides, workspace_overrides, config_defaults) -> dict:
    """The pure half of the ``core/auth/*`` resolution, shared with Node through the fixture.

    ENV -> workspace row -> default row -> ``config.json`` -> code default. An ``agent_view``
    row is never an input: a view-scoped security bound would let whoever configures a view
    widen it. A present value that is not a positive integer is an error, never a fallback,
    and the result is clamped to the ceiling in code — config can only narrow.
    """
    caps = {}
    for key in AUTH_TTL_KEYS:
        path = auth_ttl_path(key)
        raw = env.get(auth_ttl_env_key(key))
        if raw is None:
            raw = workspace_overrides.get(path)
        if raw is None:
            raw = default_overrides.get(path)
        if raw is None:
            raw = config_defaults.get(f"auth/{key}", TTL_DEFAULTS[key])
        text = str(raw).strip() if not isinstance(raw, bool) else ""
        if not _POSITIVE_DECIMAL.match(text):
            raise AuthConfigError(f"{path} must be a positive integer number of seconds")
        value = int(text)
        if value > TTL_CEILINGS[key] and key not in _clamp_warned:
            _clamp_warned.add(key)
            logger.warning("%s=%s exceeds the hard ceiling; clamped to %s", path, value, TTL_CEILINGS[key])
        caps[key] = min(value, TTL_CEILINGS[key])
    return caps


def resolve_auth_ttls(conn, workspace_id: int | None) -> dict:
    """Resolve the three ``core/auth/*`` bounds for a workspace, strictly.

    A failed query raises (issuance refuses) instead of quietly reading as "unset", which
    would turn a narrowed TTL back into the default.
    """
    from .bootstrap import CORE_MODULES_DIR
    from .config_resolver import read_config_defaults
    from .scoped_config import Scope, load_scoped_db_overrides

    def _plain(rows):
        out = {}
        for path, (value, encrypted) in rows.items():
            if path.startswith("core/auth/"):
                if encrypted:
                    raise AuthConfigError(f"{path} must not be stored encrypted")
                out[path] = value
        return out

    default_rows = _plain(load_scoped_db_overrides(conn, Scope.DEFAULT, 0, strict=True))
    workspace_rows = {}
    if workspace_id is not None:
        workspace_rows = _plain(
            load_scoped_db_overrides(conn, Scope.WORKSPACE, workspace_id, strict=True)
        )
    return compute_auth_ttls(
        env=os.environ,
        default_overrides=default_rows,
        workspace_overrides=workspace_rows,
        config_defaults=read_config_defaults(Path(CORE_MODULES_DIR) / "core"),
    )
