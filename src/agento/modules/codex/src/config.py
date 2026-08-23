"""Codex CLI config writer — .codex/config.toml with MCP servers."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agento.framework.agent_manager.credential_store import update_refreshed_credentials
from agento.framework.agent_manager.errors import AuthenticationError
from agento.framework.harness import ToolboxConnectionSpec

if TYPE_CHECKING:
    import pymysql

    from agento.framework.agent_manager.models import CredentialRecord

logger = logging.getLogger(__name__)


# A credential claiming to expire before 2024 predates the OAuth flow entirely — garbage,
# not "expired long ago" (the two are otherwise indistinguishable).
_MIN_PLAUSIBLE_EPOCH_SECONDS = 1_704_067_200  # 2024-01-01T00:00:00Z


def _ttl_from_jwt_exp(access_token: object) -> int | None:
    """Seconds until the ``exp`` claim of our own access-token JWT. ``None`` if unusable.

    The signature is deliberately NOT verified: this is our own token, read only to decide
    whether to serialize its refresh — never to authorize anything. An opaque (non-JWT)
    access token simply yields ``None``, which the framework treats as refresh-imminent.
    Never raises.
    """
    if not isinstance(access_token, str):
        return None
    parts = access_token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)  # base64url needs its padding restored
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    if exp < _MIN_PLAUSIBLE_EPOCH_SECONDS:
        return None
    return int(exp - datetime.now(UTC).timestamp())


def _ttl_from_iso(raw: object) -> int | None:
    """Seconds until an ISO-8601 ``tokens.expiry`` (the shape workspace_build reads).

    A naive timestamp is read as UTC, matching the rest of the framework. Never raises.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() - datetime.now(UTC).timestamp())


def _derive_mcp_type(url: str) -> str:
    """Derive MCP server type from URL path."""
    if "/mcp" in url:
        return "streamable_http"
    if "/sse" in url:
        return "sse"
    logger.warning("Cannot derive MCP type from URL %r, falling back to sse", url)
    return "sse"


_BARE_TOML_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge TOML-like nested dicts, with override winning."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _toml_quote_key(key: str) -> str:
    if _BARE_TOML_KEY_RE.match(key):
        return key
    return json.dumps(key)


def _toml_literal(value: str | bool | int | float) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _dump_toml(data: dict) -> str:
    """Serialize the small TOML subset used by Codex config files."""
    lines: list[str] = []

    def emit_table(table: dict, path: tuple[str, ...]) -> None:
        scalar_keys = sorted(k for k, v in table.items() if not isinstance(v, dict))
        child_keys = sorted(k for k, v in table.items() if isinstance(v, dict))

        if path:
            if lines and lines[-1] != "":
                lines.append("")
            header = ".".join(_toml_quote_key(part) for part in path)
            lines.append(f"[{header}]")

        for key in scalar_keys:
            lines.append(f"{_toml_quote_key(key)} = {_toml_literal(table[key])}")

        for key in child_keys:
            emit_table(table[key], (*path, key))

    root_scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    root_tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    for key in sorted(root_scalars):
        lines.append(f"{_toml_quote_key(key)} = {_toml_literal(root_scalars[key])}")
    for key in sorted(root_tables):
        emit_table(root_tables[key], (key,))
    return "\n".join(lines) + ("\n" if lines else "")


def _strip_toml_table(text: str, table: str) -> str:
    """Remove ``table`` and its key/value lines from ``text``, keeping everything else.

    Deliberately line-based rather than a full TOML round-trip: rewriting the file through a
    parser would reformat and reorder an operator's hand-edited config.toml, and this only
    needs to replace one table it owns.
    """
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped == table
        if not skipping:
            out.append(line)
    return "\n".join(out)


class CodexWorkspaceAdapter:
    """Writes Codex CLI config: .codex/config.toml with MCP servers block."""

    def owned_paths(self) -> tuple[set[str], set[str]]:
        return set(), {".codex"}

    def persistent_home_paths(self) -> list[str]:
        """Codex session + history state that must survive workspace rebuilds."""
        return [".codex/history.jsonl", ".codex/sessions"]

    def credential_env(self, credential: CredentialRecord) -> dict[str, str]:
        if credential.type == "openai_api_key":
            credentials = credential.credentials or {}
            if not credentials.get("api_key"):
                raise ValueError(
                    f"Credential id={credential.id} label={credential.label!r} is typed "
                    "'openai_api_key' but credentials['api_key'] is missing or empty."
                )
            # Codex CLI does not treat OPENAI_API_KEY as runtime auth in a
            # clean HOME. API-key tokens are materialized into auth.json by
            # write_credentials(), same as access-credential/OAuth credentials.
            return {}
        # oauth + codex_access_token both rely on .codex/auth.json on disk.
        return {}

    def write_credentials(self, build_dir: Path, credential: CredentialRecord) -> None:
        """Materialize Codex auth based on credential.type.

        - codex_access_token: shell out to ``codex login --with-access-token``
          with ``HOME=<target_dir>`` and the JWT on stdin so Codex itself
          writes the correct auth.json shape.
        - openai_api_key: shell out to ``codex login --with-api-key`` with
          ``HOME=<target_dir>`` and the API key on stdin so Codex writes the
          auth.json shape it requires.
        - oauth (default): write the captured raw_auth verbatim to
          ``.codex/auth.json``.
        """
        if credential.type == "codex_access_token":
            access_token = (credential.credentials or {}).get("access_token")
            if not access_token:
                raise AuthenticationError(
                    f"Credential id={credential.id} label={credential.label!r} is typed "
                    "'codex_access_token' but credentials['access_token'] is missing or empty."
                )
            self._login_with_access_token(build_dir, access_token)
            return

        if credential.type == "openai_api_key":
            api_key = (credential.credentials or {}).get("api_key")
            if not api_key:
                raise AuthenticationError(
                    f"Credential id={credential.id} label={credential.label!r} is typed "
                    "'openai_api_key' but credentials['api_key'] is missing or empty."
                )
            self._login_with_api_key(build_dir, api_key)
            return

        # oauth (default)
        credentials = credential.credentials or {}
        raw_auth = credentials.get("raw_auth")
        if not raw_auth:
            logger.warning(
                "Codex OAuth credentials missing raw_auth; skipping .codex/auth.json "
                "— agent will need to /login on first run."
            )
            return
        codex_dir = build_dir / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        path = codex_dir / "auth.json"
        path.write_text(json.dumps(raw_auth, indent=2))
        os.chmod(path, 0o600)
        logger.debug("Wrote Codex OAuth credentials to %s", path)

    def _login_with_access_token(self, build_dir: Path, token_str: str) -> None:
        """Run `codex login --with-access-token` with HOME=<target_dir>; the token
        is piped via stdin so it never appears in argv or env."""
        build_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "HOME": str(build_dir)}
        try:
            result = subprocess.run(
                ["codex", "login", "--with-access-token"],
                input=token_str, env=env, text=True,
                capture_output=True, check=False,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise AuthenticationError(
                "Codex CLI not found on PATH; cannot materialize access-credential auth.json."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AuthenticationError(
                "codex login --with-access-token timed out after 30s."
            ) from exc

        if result.returncode != 0:
            stderr_snippet = (result.stderr or "")[:300]
            logger.warning(
                "codex login --with-access-token exited %d: %s",
                result.returncode, stderr_snippet,
            )
            raise AuthenticationError(
                f"codex login --with-access-token failed (exit {result.returncode}): "
                f"{stderr_snippet}"
            )
        logger.debug(
            "Materialized Codex access-credential auth.json via codex login (HOME=%s)",
            build_dir,
        )

    def _login_with_api_key(self, build_dir: Path, api_key: str) -> None:
        """Run `codex login --with-api-key` with HOME=<target_dir>; key is
        piped via stdin so it never appears in argv or env."""
        build_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "HOME": str(build_dir)}
        try:
            result = subprocess.run(
                ["codex", "login", "--with-api-key"],
                input=api_key, env=env, text=True,
                capture_output=True, check=False,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise AuthenticationError(
                "Codex CLI not found on PATH; cannot materialize API-key auth.json."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AuthenticationError(
                "codex login --with-api-key timed out after 30s."
            ) from exc

        if result.returncode != 0:
            stderr_snippet = (result.stderr or "")[:300]
            logger.warning(
                "codex login --with-api-key exited %d: %s",
                result.returncode, stderr_snippet,
            )
            raise AuthenticationError(
                f"codex login --with-api-key failed (exit {result.returncode}): "
                f"{stderr_snippet}"
            )
        logger.debug(
            "Materialized Codex API-key auth.json via codex login (HOME=%s)",
            build_dir,
        )

    def migrate_legacy_workspace_config(self, build_dir: Path, workspace_root: Path) -> None:
        """Merge legacy shared-HOME ``workspace/.codex/config.toml`` into the build.

        The per-agent HOME migration stopped Codex from seeing MCP servers stored in
        the old shared workspace config. Preserve those entries in the new build so
        existing installs keep working until everything is moved to scoped DB config.
        """
        legacy_path = workspace_root / ".codex" / "config.toml"
        if not legacy_path.is_file():
            return

        try:
            legacy_data = tomllib.loads(legacy_path.read_text())
        except Exception:
            logger.warning("Failed to parse legacy Codex config at %s", legacy_path, exc_info=True)
            return

        codex_dir = build_dir / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        build_path = codex_dir / "config.toml"
        if build_path.is_file():
            try:
                build_data = tomllib.loads(build_path.read_text())
            except Exception:
                logger.warning("Failed to parse build Codex config at %s", build_path, exc_info=True)
                build_data = {}
        else:
            build_data = {}

        merged = _deep_merge(legacy_data, build_data)
        build_path.write_text(_dump_toml(merged))
        logger.debug("Merged legacy Codex config from %s into %s", legacy_path, build_path)

    def prepare_workspace(
        self,
        working_dir: Path,
        agent_config: dict[str, str],
        *,
        agent_view_id: int | None = None,
        toolbox_url: str,
        harness_config: dict[str, str] | None = None,
    ) -> None:
        lines: list[str] = []

        model = agent_config.get("model")
        if model:
            lines.append(f'model = "{model}"')

        approval_mode = agent_config.get("codex/approval_mode")
        if approval_mode:
            lines.append(f'approval_mode = "{approval_mode}"')

        # Auto-inject the toolbox MCP entry; operators can add more (or shadow
        # "toolbox") via agent_view/mcp/servers.
        servers: dict[str, dict] = {
            "toolbox": {"url": f"{toolbox_url.rstrip('/')}/mcp"},
        }
        extra_raw = agent_config.get("mcp/servers")
        if extra_raw:
            try:
                extra = json.loads(extra_raw)
                if isinstance(extra, dict):
                    servers.update(extra)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid JSON in agent_view/mcp/servers, ignoring extras")

        for name, server_cfg in servers.items():
            url = server_cfg.get("url", "")
            if agent_view_id is not None and ("/sse" in url or "/mcp" in url):
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}agent_view_id={agent_view_id}"
            mcp_type = _derive_mcp_type(url)
            lines.append(f"\n[mcp_servers.{name}]")
            lines.append(f'type = "{mcp_type}"')
            lines.append(f'url = "{url}"')

        codex_dir = working_dir / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        config_path = codex_dir / "config.toml"
        config_path.write_text("\n".join(lines) + "\n")
        logger.debug("Generated %s", config_path)

    def inject_runtime_params(
        self,
        artifacts_dir: Path,
        *,
        job_id: int | None,
    ) -> None:
        """Scope the copied config to one job.

        ``job_id=None`` means the run has no job scope (a string-id ``agento run``). There
        is nothing to scope then, so return early rather than render the literal "None"
        into the config. The framework does not currently make this call for this adapter —
        it only passes ``None`` to adapters that name an ``effective_*`` override keyword —
        but the Protocol permits it, so honouring it here keeps the declared type true.
        """
        if job_id is None:
            return
        config_path = artifacts_dir / ".codex" / "config.toml"
        if not config_path.is_file():
            return
        try:
            data = tomllib.loads(config_path.read_text())
        except Exception:
            return
        mcp_servers = data.get("mcp_servers", {})
        if not mcp_servers:
            return

        for server_cfg in mcp_servers.values():
            url = server_cfg.get("url", "")
            if "/sse" in url or "/mcp" in url:
                sep = "&" if "?" in url else "?"
                server_cfg["url"] = f"{url}{sep}job_id={job_id}"

        # Re-write the TOML (hand-written, simple structure)
        lines: list[str] = []
        model = data.get("model")
        if model:
            lines.append(f'model = "{model}"')
        approval_mode = data.get("approval_mode")
        if approval_mode:
            lines.append(f'approval_mode = "{approval_mode}"')

        for name, server_cfg in mcp_servers.items():
            lines.append(f"\n[mcp_servers.{name}]")
            lines.append(f'type = "{server_cfg.get("type", "sse")}"')
            lines.append(f'url = "{server_cfg.get("url", "")}"')

        config_path.write_text("\n".join(lines) + "\n")

    def remove_credentials(self, target_dir: Path) -> None:
        """Drop Codex's login state, keeping `.codex/config.toml` intact.

        Only `auth.json` holds credentials; config.toml carries model/MCP settings the
        login flow still needs.
        """
        auth_path = target_dir / ".codex" / "auth.json"
        auth_path.unlink(missing_ok=True)
        logger.debug("Removed Codex credential state from %s", target_dir)

    def credential_ttl_seconds(self, credential: CredentialRecord) -> int | None:
        """Seconds until this credential's access token expires, or ``None`` if unknown.

        Part of the ``WorkspaceAdapter`` protocol. Codex stores no expiry field of its
        own, so the primary source is the ``exp`` claim of its own access-token JWT; the
        fallback is the ISO ``tokens.expiry`` shape the workspace builder already reads.
        Giving codex an exact expiry keeps the conservative "unknown ⇒ exclusive" path
        rare instead of permanent.
        """
        if credential.type != "oauth":
            return None
        tokens = (credential.credentials or {}).get("raw_auth")
        tokens = tokens.get("tokens") if isinstance(tokens, dict) else None
        if not isinstance(tokens, dict):
            return None
        ttl = _ttl_from_jwt_exp(tokens.get("access_token"))
        return ttl if ttl is not None else _ttl_from_iso(tokens.get("expiry"))

    def capture_refreshed_credentials(
        self,
        home_dir: Path,
        credential: CredentialRecord,
        conn: pymysql.Connection,
    ) -> bool:
        """Persist the Codex CLI's on-disk credential rotation back to ``credential``.

        Returns ``True`` when something was persisted, per the ``WorkspaceAdapter``
        protocol — every early exit below is a "nothing to capture" case. The framework
        also uses it to detect a rotation that happened WITHOUT a refresh lease.
        """
        # Only OAuth tokens have a refresh_token Codex CLI might rotate.
        # API-key and access-credential rows never produce a meaningful auth.json
        # diff we should persist back.
        if credential.type != "oauth":
            return False
        auth_path = home_dir / ".codex" / "auth.json"
        if not auth_path.is_file():
            return False

        try:
            refreshed = json.loads(auth_path.read_text())
        except Exception:
            logger.warning("Failed to read refreshed auth.json at %s", auth_path, exc_info=True)
            return False

        old_refresh = (credential.credentials or {}).get("raw_auth", {}).get("tokens", {}).get("refresh_token")
        new_refresh = refreshed.get("tokens", {}).get("refresh_token")

        if not new_refresh or new_refresh == old_refresh:
            return False

        new_creds = dict(credential.credentials or {})
        new_creds["raw_auth"] = refreshed
        tokens = refreshed.get("tokens", {})
        if "refresh_token" in tokens:
            new_creds["refresh_token"] = tokens["refresh_token"]
        if "access_token" in tokens:
            new_creds["subscription_key"] = tokens["access_token"]

        # Capture-specific persistence: preserves operator/health state (does NOT
        # re-enable a credential an operator disabled mid-run). Now that the consumer
        # hook commits the capture, register_credential would silently resurrect it.
        update_refreshed_credentials(conn, credential.id, new_creds, logger=logger)
        return True

    def serialize_toolbox_connection(
        self, spec: ToolboxConnectionSpec, target_dir: Path
    ) -> None:
        """Write the Toolbox entry into Codex's ``.codex/config.toml``.

        How a connection is materialized is entirely the harness's business — the framework
        hands over a plain :class:`ToolboxConnectionSpec` and makes no assumption that a
        harness even has an MCP config file.

        **Idempotent**: an existing ``[mcp_servers.<name>]`` table for this spec is REPLACED,
        not appended to. Appending produced duplicate tables on a second call, and Codex takes
        whichever it parses last — so a stale URL could win.

        ``spec.headers`` is serialized too (``http_headers``); dropping it would silently lose
        the Toolbox's auth on any deployment that sets one.
        """
        config_path = target_dir / ".codex" / "config.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        table = f"[mcp_servers.{_toml_quote_key(spec.name)}]"
        body = [
            table,
            f"type = {_toml_literal(spec.transport)}",
            f"url = {_toml_literal(spec.url)}",
        ]
        if spec.headers:
            inline = ", ".join(
                f"{_toml_quote_key(k)} = {_toml_literal(v)}"
                for k, v in sorted(spec.headers.items())
            )
            body.append(f"http_headers = {{ {inline} }}")

        existing = config_path.read_text() if config_path.is_file() else ""
        kept = _strip_toml_table(existing, table)
        parts = [kept.rstrip("\n")] if kept.strip() else []
        parts.append("\n".join(body))
        config_path.write_text("\n\n".join(parts).lstrip("\n") + "\n")
