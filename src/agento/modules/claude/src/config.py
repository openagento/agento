"""Claude Code CLI config writer — .claude.json, .claude/settings.json, .mcp.json."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from agento.framework.agent_manager.credential_store import update_refreshed_credentials
from agento.framework.harness import ToolboxConnectionSpec

if TYPE_CHECKING:
    import pymysql

    from agento.framework.agent_manager.models import CredentialRecord

logger = logging.getLogger(__name__)

# Keys copied from the captured developer ``~/.claude.json`` into the agent's
# build ``.claude.json``. Strictly auth/identity restoration — without
# ``oauthAccount`` Claude falls into the login picker even with a valid
# ``.credentials.json``. Everything else in the developer payload is per-CWD
# or per-machine state (notably ``projects``, whose
# ``enabledMcpjsonServers: []`` silently disables MCP servers including the
# toolbox auto-injected by ``prepare_workspace``).
_AUTH_IDENTITY_KEYS = frozenset({
    "oauthAccount",
    "userID",
    "numStartups",
    "firstStartTime",
    "hasCompletedOnboarding",
})


def _derive_mcp_type(name: str, server_cfg: dict) -> str | None:
    """Infer the ``.mcp.json`` type discriminator for an entry that omits it.

    Claude Code validates each entry on ``type``; a typeless URL entry defaults
    to ``stdio``, then fails for lacking ``command``, and the server is dropped.
    Warnings identify the entry by name only — operator URLs may carry
    credentials (userinfo or a query credential).
    """
    if "command" in server_cfg:
        return "stdio"
    url = server_cfg.get("url")
    if not isinstance(url, str) or not url:
        return None
    try:
        path = urlsplit(url).path.rstrip("/")
    except ValueError:
        path = ""
    if path.endswith("/sse"):
        return "sse"
    if path.endswith("/mcp"):
        return "http"
    logger.warning("MCP server %r: cannot derive type from URL, defaulting to http", name)
    return "http"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except Exception:
        logger.warning("Failed to parse JSON config at %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _merge_json(legacy: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = dict(legacy)
    for key, value in current.items():
        if key == "enabledMcpjsonServers":
            legacy_list = legacy.get(key, [])
            current_list = value
            if isinstance(legacy_list, list) and isinstance(current_list, list):
                merged[key] = list(dict.fromkeys([*legacy_list, *current_list]))
                continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_json(merged[key], value)
        else:
            merged[key] = value
    return merged


class ClaudeWorkspaceAdapter:
    """Writes Claude Code CLI config files: .claude.json, .claude/settings.json, .mcp.json."""

    def owned_paths(self) -> tuple[set[str], set[str]]:
        return {".claude.json", ".mcp.json"}, {".claude"}

    def persistent_home_paths(self) -> list[str]:
        """Claude Code session + todo state that must survive workspace rebuilds."""
        return [".claude/projects", ".claude/todos"]

    def credential_env(self, credential: CredentialRecord) -> dict[str, str]:
        if credential.type == "anthropic_api_key":
            credentials = credential.credentials or {}
            api_key = credentials.get("api_key")
            if not api_key:
                raise ValueError(
                    f"Credential id={credential.id} label={credential.label!r} is typed "
                    "'anthropic_api_key' but credentials['api_key'] is missing or empty."
                )
            return {"ANTHROPIC_API_KEY": api_key}
        return {}

    def write_credentials(self, build_dir: Path, credential: CredentialRecord) -> None:
        """Materialize Claude Code's full login state into ``build_dir``.

        Writes ``.claude/.credentials.json`` (oauth tokens) and merges Claude's
        captured ``~/.claude.json`` into ``build_dir/.claude.json`` (so Claude
        sees ``oauthAccount`` and considers itself logged in on first run).
        Preserves any agent_view-level keys already in ``.claude.json`` such as
        ``model``/``systemPrompt``/``permissions`` written by ``prepare_workspace``.
        """
        credentials = credential.credentials or {}
        if credential.type == "anthropic_api_key":
            if not credentials.get("api_key"):
                raise ValueError(
                    f"Credential id={credential.id} label={credential.label!r} is typed "
                    "'anthropic_api_key' but credentials['api_key'] is missing or empty."
                )
            self._clear_oauth_state(build_dir)
            return

        self._remove_claude_json_backups(build_dir)

        raw_auth = credentials.get("raw_auth") or {}
        raw_creds = raw_auth.get("credentials") if isinstance(raw_auth, dict) else None
        claude_oauth = (raw_creds or {}).get("claudeAiOauth") if isinstance(raw_creds, dict) else None

        if isinstance(claude_oauth, dict) and claude_oauth.get("accessToken"):
            # Preferred: write back Claude's full payload verbatim so fields like
            # ``scopes`` and ``rateLimitTier`` survive. Anything less trips Claude
            # into the login picker even with a valid accessToken.
            creds_payload = raw_creds
        else:
            # Backwards compat for tokens stored before raw_auth capture, and for
            # file-based ``credential:register`` using the legacy 4-field schema.
            access_token = credentials.get("subscription_key")
            if not access_token:
                self._clear_oauth_state(build_dir)
                return
            creds_payload = {
                "claudeAiOauth": {
                    "accessToken": access_token,
                    "refreshToken": credentials.get("refresh_token"),
                    "expiresAt": credentials.get("expires_at"),
                    "subscriptionType": credentials.get("subscription_type"),
                }
            }

        claude_dir = build_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        creds_path = claude_dir / ".credentials.json"
        creds_path.write_text(json.dumps(creds_payload, indent=2) + "\n")
        os.chmod(creds_path, 0o600)
        logger.debug("Wrote Claude credentials to %s", creds_path)

        claude_json_payload = raw_auth.get("claude_json") if isinstance(raw_auth, dict) else None
        if not isinstance(claude_json_payload, dict) or not claude_json_payload:
            self._strip_oauth_identity_state(build_dir)
            return
        sanitized = {
            k: v for k, v in claude_json_payload.items()
            if k in _AUTH_IDENTITY_KEYS
        }
        if not sanitized:
            self._strip_oauth_identity_state(build_dir)
            return
        claude_json_path = build_dir / ".claude.json"
        existing: dict[str, Any] = {}
        if claude_json_path.is_file():
            try:
                parsed = json.loads(claude_json_path.read_text())
                if isinstance(parsed, dict):
                    existing = parsed
            except (json.JSONDecodeError, OSError):
                existing = {}
        merged = {**existing, **sanitized}
        claude_json_path.write_text(json.dumps(merged, indent=2) + "\n")
        logger.debug("Wrote Claude user state to %s", claude_json_path)

    def remove_credentials(self, target_dir: Path) -> None:
        """Drop Claude's login state, keeping `.claude.json` config intact.

        Reuses the same clearing path `write_credentials` uses for a credential with no
        usable payload, so the two cannot drift.
        """
        self._clear_oauth_state(target_dir)
        logger.debug("Removed Claude credential state from %s", target_dir)

    def _clear_oauth_state(self, build_dir: Path) -> None:
        creds_path = build_dir / ".claude" / ".credentials.json"
        creds_path.unlink(missing_ok=True)
        self._strip_oauth_identity_state(build_dir)

    def _remove_claude_json_backups(self, build_dir: Path) -> None:
        backups_dir = build_dir / ".claude" / "backups"
        if backups_dir.is_dir():
            for backup in backups_dir.glob(".claude.json.backup.*"):
                if backup.is_file() or backup.is_symlink():
                    backup.unlink(missing_ok=True)

    def _strip_oauth_identity_state(self, build_dir: Path) -> None:
        self._remove_claude_json_backups(build_dir)

        claude_json_path = build_dir / ".claude.json"
        if not claude_json_path.exists():
            return
        try:
            existing = json.loads(claude_json_path.read_text())
        except (json.JSONDecodeError, OSError):
            claude_json_path.unlink(missing_ok=True)
            return
        if not isinstance(existing, dict):
            claude_json_path.unlink(missing_ok=True)
            return

        cleaned = {
            key: value for key, value in existing.items()
            if key not in _AUTH_IDENTITY_KEYS
        }
        if cleaned:
            claude_json_path.write_text(json.dumps(cleaned, indent=2) + "\n")
        else:
            claude_json_path.unlink(missing_ok=True)

    def migrate_legacy_workspace_config(self, build_dir: Path, workspace_root: Path) -> None:
        """Merge legacy shared-HOME Claude config into the per-agent build."""
        legacy_files = [
            ".claude.json",
            ".mcp.json",
            ".claude/settings.json",
            ".claude/settings.local.json",
        ]
        for rel in legacy_files:
            legacy_path = workspace_root / rel
            if not legacy_path.is_file():
                continue
            build_path = build_dir / rel
            build_path.parent.mkdir(parents=True, exist_ok=True)
            if not build_path.is_file():
                build_path.write_text(legacy_path.read_text())
                continue
            merged = _merge_json(_load_json(legacy_path), _load_json(build_path))
            build_path.write_text(json.dumps(merged, indent=2) + "\n")

    def prepare_workspace(
        self,
        working_dir: Path,
        agent_config: dict[str, str],
        *,
        agent_view_id: int | None = None,
        toolbox_url: str,
    ) -> None:
        working_dir.mkdir(parents=True, exist_ok=True)
        self._write_claude_json(working_dir, agent_config)
        self._write_settings_json(working_dir, agent_config)
        self._write_mcp_json(
            working_dir, agent_config,
            agent_view_id=agent_view_id,
            toolbox_url=toolbox_url,
        )

    def inject_runtime_params(
        self,
        artifacts_dir: Path,
        *,
        job_id: int,
    ) -> None:
        mcp_path = artifacts_dir / ".mcp.json"
        if not mcp_path.is_file():
            return
        try:
            data = json.loads(mcp_path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        servers = data.get("mcpServers", {})
        for server_cfg in servers.values():
            if not isinstance(server_cfg, dict):
                continue
            url = server_cfg.get("url")
            if isinstance(url, str) and ("/sse" in url or "/mcp" in url):
                sep = "&" if "?" in url else "?"
                server_cfg["url"] = f"{url}{sep}job_id={job_id}"
        mcp_path.write_text(json.dumps(data, indent=2))

    def capture_refreshed_credentials(
        self,
        home_dir: Path,
        credential: CredentialRecord,
        conn: pymysql.Connection,
    ) -> bool:
        """Persist the Claude CLI's on-disk credential rotation back to ``credential``.

        On a real ``refreshToken`` rotation, write the refreshed
        ``.claude/.credentials.json`` back so the credential self-heals instead of
        replaying a now-invalidated single-use credential → ``401``. Parity with
        ``CodexWorkspaceAdapter.capture_refreshed_credentials``; full rationale
        (incl. why ``expires_at`` is forced NULL) in DECISIONS.md.

        Returns ``True`` when something was persisted, per the ``WorkspaceAdapter``
        protocol — every early exit below is a "nothing to capture" case.
        """
        # Only OAuth subscription tokens carry a refresh_token the CLI rotates.
        # anthropic_api_key rows never produce a credentials diff worth persisting.
        if credential.type != "oauth":
            return False

        creds_path = home_dir / ".claude" / ".credentials.json"
        if not creds_path.is_file():
            return False
        try:
            refreshed = json.loads(creds_path.read_text())
        except Exception:
            logger.warning(
                "Failed to read refreshed .credentials.json at %s", creds_path, exc_info=True
            )
            return False

        new_oauth = refreshed.get("claudeAiOauth") if isinstance(refreshed, dict) else None
        if not isinstance(new_oauth, dict):
            return False
        new_refresh = new_oauth.get("refreshToken")
        if not new_refresh:
            return False

        # Old refresh credential: prefer the captured raw_auth shape; fall back to the
        # legacy top-level field for tokens registered before raw_auth capture,
        # so an un-rotated legacy credential does not trigger a spurious write.
        credentials = credential.credentials or {}
        old_raw_auth = credentials.get("raw_auth")
        old_raw_auth = old_raw_auth if isinstance(old_raw_auth, dict) else {}
        old_creds = old_raw_auth.get("credentials")
        old_refresh = None
        if isinstance(old_creds, dict):
            old_oauth = old_creds.get("claudeAiOauth")
            if isinstance(old_oauth, dict):
                old_refresh = old_oauth.get("refreshToken")
        if old_refresh is None:
            old_refresh = credentials.get("refresh_token")

        if new_refresh == old_refresh:
            return False

        new_creds = dict(credentials)
        raw_auth = dict(old_raw_auth)
        raw_auth["credentials"] = refreshed  # full file verbatim; preserves claude_json
        new_creds["raw_auth"] = raw_auth
        new_creds["refresh_token"] = new_refresh
        if new_oauth.get("accessToken"):
            new_creds["subscription_key"] = new_oauth["accessToken"]
        # Force the DB row's expires_at NULL: select_credential treats it as a hard
        # "unusable after" filter and nothing refreshes outside a job run, so a
        # stored expiry would retire a still-refreshable credential after an idle gap
        # and re-break this self-heal. Explicit (not relying on ms→None coercion);
        # the CLI still reads the real expiresAt from the file in raw_auth.
        new_creds["expires_at"] = None

        # Capture-specific persistence: updates credentials by id and preserves
        # operator/health state — NOT register_credential (which would re-enable a
        # credential an operator disabled mid-run). See DECISIONS.md.
        update_refreshed_credentials(conn, credential.id, new_creds, logger=logger)
        return True

    @staticmethod
    def _write_claude_json(working_dir: Path, agent_config: dict[str, str]) -> None:
        claude_json: dict[str, Any] = {}

        model = agent_config.get("model")
        if model:
            claude_json["model"] = model

        personality = agent_config.get("claude/personality")
        if personality:
            claude_json["systemPrompt"] = personality

        permissions = agent_config.get("claude/permissions")
        if permissions:
            try:
                claude_json["permissions"] = json.loads(permissions)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid JSON in agent_view/claude/permissions, skipping")

        if claude_json:
            config_path = working_dir / ".claude.json"
            config_path.write_text(json.dumps(claude_json, indent=2) + "\n")
            logger.debug("Generated %s", config_path)

    @staticmethod
    def _write_settings_json(working_dir: Path, agent_config: dict[str, str]) -> None:
        settings: dict[str, Any] = {}

        trust_level = agent_config.get("claude/trust_level")
        if trust_level:
            settings["permissions"] = {"dangerouslySkipPermissions": trust_level == "full"}

        if settings:
            settings_dir = working_dir / ".claude"
            settings_dir.mkdir(parents=True, exist_ok=True)
            settings_path = settings_dir / "settings.json"
            settings_path.write_text(json.dumps(settings, indent=2) + "\n")
            logger.debug("Generated %s", settings_path)

    @staticmethod
    def _write_mcp_json(
        working_dir: Path,
        agent_config: dict[str, str],
        *,
        agent_view_id: int | None = None,
        toolbox_url: str,
    ) -> None:
        # Auto-inject the toolbox MCP entry; operators can add more (or shadow
        # "toolbox") via agent_view/mcp/servers.
        # alwaysLoad: connect this server *before* the CLI emits system/init.
        # Claude connects MCP servers non-blocking by default, so without it the
        # init self-report always reads "pending" and job.toolbox_mcp_connected
        # can never be TRUE. Only our own local toolbox gets this — blocking on
        # an operator's third-party MCP server would delay every job start.
        servers: dict[str, dict] = {
            "toolbox": {
                "type": "http",
                "url": f"{toolbox_url.rstrip('/')}/mcp",
                "alwaysLoad": True,
            },
        }
        extra_raw = agent_config.get("mcp/servers")
        if extra_raw:
            extra: Any = {}
            try:
                extra = json.loads(extra_raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid JSON in agent_view/mcp/servers, ignoring extras")
            if isinstance(extra, dict):
                for name, server_cfg in extra.items():
                    if not isinstance(server_cfg, dict):
                        logger.warning("MCP server %r is not an object, ignoring", name)
                        continue
                    server_cfg = dict(server_cfg)
                    if "type" not in server_cfg:
                        derived = _derive_mcp_type(name, server_cfg)
                        if derived is None:
                            logger.warning(
                                "MCP server %r has no type and none can be derived, ignoring",
                                name,
                            )
                            continue
                        server_cfg["type"] = derived
                    servers[name] = server_cfg

        if agent_view_id is not None:
            for server_cfg in servers.values():
                url = server_cfg.get("url")
                if isinstance(url, str) and ("/sse" in url or "/mcp" in url):
                    sep = "&" if "?" in url else "?"
                    server_cfg["url"] = f"{url}{sep}agent_view_id={agent_view_id}"

        mcp_config = {"mcpServers": servers}
        config_path = working_dir / ".mcp.json"
        config_path.write_text(json.dumps(mcp_config, indent=2) + "\n")
        logger.debug("Generated %s", config_path)

    def serialize_toolbox_connection(
        self, spec: ToolboxConnectionSpec, target_dir: Path
    ) -> None:
        """Write the Toolbox entry into Claude's ``.mcp.json``.

        How a connection is materialized is entirely the harness's business — the
        framework hands over a plain :class:`ToolboxConnectionSpec` and makes no
        assumption that a harness even has an MCP config file.
        """
        mcp_path = target_dir / ".mcp.json"
        data: dict[str, Any] = _load_json(mcp_path)
        servers = data.setdefault("mcpServers", {})
        entry: dict[str, Any] = {"type": spec.transport, "url": spec.url, "alwaysLoad": True}
        if spec.headers:
            entry["headers"] = dict(spec.headers)
        servers[spec.name] = entry
        mcp_path.write_text(json.dumps(data, indent=2))
