"""Core build logic for composable workspace builds.

Materializes a pre-built directory per agent_view containing all config files,
instruction files, and skills. The consumer can then copy from the build dir
instead of regenerating everything per job.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agento.framework.events import (
    WorkspaceBuildCompletedEvent,
    WorkspaceBuildFailedEvent,
    WorkspaceBuildStartedEvent,
)
from agento.framework.git_identity import (
    GIT_AUTHOR_EMAIL_PATH,
    GIT_AUTHOR_NAME_PATH,
    gitconfig_user_block,
)
from agento.framework.persistent_home import ensure_state_dir as _ensure_state_dir
from agento.framework.persistent_home import link_persistent_paths
from agento.framework.ssh_keys import EncryptedKeyError, derive_public_key
from agento.framework.workspace_paths import BUILD_DIR, THEME_DIR

_DEFAULT_MAX_BUILDS = 10

# Config paths (scoped, from agent_view/workspace/global)
_SSH_PRIVATE_KEY_PATH = "agent_view/identity/ssh_private_key"
_SSH_PUBLIC_KEY_PATH = "agent_view/identity/ssh_public_key"
_SSH_CONFIG_PATH = "agent_view/identity/ssh_config"
_SSH_KNOWN_HOSTS_PATH = "agent_view/identity/ssh_known_hosts"

logger = logging.getLogger(__name__)


def _dispatch(event_name: str, event: object) -> None:
    """Dispatch event via framework EventManager (swallows errors if not bootstrapped)."""
    try:
        from agento.framework.event_manager import get_event_manager
        get_event_manager().dispatch(event_name, event)
    except Exception:
        logger.debug("Event dispatch skipped for %s", event_name)

CLAUDE_MD_CONTENT = "# Instructions\n\nPlease read and follow [AGENTS.md](AGENTS.md).\n"

# Bump when the on-disk build layout changes in a backwards-incompatible way;
# mixed into the checksum so existing builds invalidate automatically on upgrade.
_SKILLS_LAYOUT_VERSION = "dir_v1"

# Recursive manifest descent cap — dirs colliding past this depth collapse to latest-wins.
_MAX_MANIFEST_DEPTH = 10

_SOURCES = ("theme", "modules", "skills")
_STRATEGY_VALUES = ("copy", "symlink")

Strategy = Literal["copy", "symlink"]
Kind = Literal["file", "dir"]

_INSTRUCTION_FILES = {
    "agent_view/instructions/agents_md": "AGENTS.md",
    "agent_view/instructions/soul_md": "SOUL.md",
}


@dataclass
class BuildResult:
    build_id: int
    build_dir: str
    checksum: str
    skipped: bool = False


def compute_build_checksum(
    resolved: dict[str, str],
    skill_checksums: list[str] | None = None,
    *,
    strategies: dict[str, str] | None = None,
) -> str:
    """Deterministic SHA-256 over sorted effective config values + skill checksums
    + per-source strategies. ``resolved`` is the ENV->DB->config.json effective
    config ({path: value}), so an ENV override drifts the checksum and rebuilds."""
    parts = []
    for path in sorted(resolved.keys()):
        parts.append(f"{path}={resolved[path]}")
    if skill_checksums:
        parts.extend(sorted(skill_checksums))
    effective = {s: "copy" for s in _SOURCES}
    if strategies:
        for k, v in strategies.items():
            if k in effective:
                effective[k] = v
    for source in _SOURCES:
        parts.append(f"__strategy/{source}={effective[source]}")
    parts.append(f"__skills_layout={_SKILLS_LAYOUT_VERSION}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _write_instruction_files(
    build_dir: Path,
    resolved: dict[str, str],
) -> None:
    """Write AGENTS.md, SOUL.md, CLAUDE.md; unlink first so a prior theme symlink is not followed."""
    for config_path, filename in _INSTRUCTION_FILES.items():
        value = resolved.get(config_path)
        if value:
            target = build_dir / filename
            target.unlink(missing_ok=True)
            target.write_text(value)
    claude_target = build_dir / "CLAUDE.md"
    claude_target.unlink(missing_ok=True)
    claude_target.write_text(CLAUDE_MD_CONTENT)


def _resolve_skills_dir() -> Path:
    """Resolve skills directory from skill module config or default."""
    try:
        from agento.framework.bootstrap import get_module_config
        cfg = get_module_config("skill")
        if cfg and isinstance(cfg, dict) and cfg.get("skills_dir"):
            return Path(cfg["skills_dir"])
    except Exception:
        pass
    return Path("workspace/.claude/skills")


def _get_enabled_skills(conn, agent_view_id, workspace_id):
    """Fetch enabled skills (soft dependency on skill module). Returns (skills, registry) or ([], None)."""
    import importlib
    try:
        registry = importlib.import_module("agento.modules.skill.src.registry")
    except (ImportError, ModuleNotFoundError):
        return [], None
    skills = registry.get_enabled_skills(conn, agent_view_id=agent_view_id, workspace_id=workspace_id)
    return skills, registry


def _kind_of(path: Path) -> Kind:
    return "dir" if path.is_dir() else "file"


def build_manifest(
    layers: list[Path | None],
    depth: int = 0,
) -> dict[str, tuple[Path, Kind]]:
    """Merge N ordered layers (base→specific) into a relative-path → (source, kind) manifest."""
    items_by_name: dict[str, list[Path]] = {}
    for layer in layers:
        if layer is None or not layer.is_dir():
            continue
        for item in layer.iterdir():
            if item.name.startswith((".", "_")):
                continue
            items_by_name.setdefault(item.name, []).append(item)

    manifest: dict[str, tuple[Path, Kind]] = {}
    for name, occurrences in items_by_name.items():
        latest = occurrences[-1]
        if len(occurrences) == 1:
            manifest[name] = (latest, _kind_of(latest))
            continue

        all_dirs = all(o.is_dir() for o in occurrences)
        if not all_dirs:
            manifest[name] = (latest, _kind_of(latest))
            continue

        if depth >= _MAX_MANIFEST_DEPTH:
            manifest[name] = (latest, "dir")
            continue

        sub = build_manifest(occurrences, depth + 1)
        for sub_rel, sub_val in sub.items():
            manifest[f"{name}/{sub_rel}"] = sub_val

    return manifest


def apply_manifest(
    manifest: dict[str, tuple[Path, Kind]],
    target_dir: Path,
    strategy: Strategy,
) -> None:
    """Write manifest entries into ``target_dir`` via copy or symlink; parent dirs are always real."""
    target_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, (src, kind) in manifest.items():
        target = target_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if strategy == "copy":
            if kind == "file":
                shutil.copy2(src, target)
            else:
                shutil.copytree(src, target)
        else:
            target.symlink_to(src.resolve())


def _theme_layers(workspace_code: str, agent_view_code: str) -> list[Path]:
    root = Path(THEME_DIR)
    if not root.is_dir():
        return []
    layers: list[Path] = [root]
    ws_dir = root / f"_{workspace_code}"
    if ws_dir.is_dir():
        layers.append(ws_dir)
        av_dir = ws_dir / f"_{agent_view_code}"
        if av_dir.is_dir():
            layers.append(av_dir)
    return layers


def _module_layers(
    module_workspace: Path,
    workspace_code: str,
    agent_view_code: str,
) -> list[Path]:
    layers: list[Path] = [module_workspace]
    ws_dir = module_workspace / f"_{workspace_code}"
    if ws_dir.is_dir():
        layers.append(ws_dir)
        av_dir = ws_dir / f"_{agent_view_code}"
        if av_dir.is_dir():
            layers.append(av_dir)
    return layers


def _copy_theme(
    build_dir: Path,
    workspace_code: str,
    agent_view_code: str,
    *,
    strategy: Strategy = "copy",
) -> None:
    layers = _theme_layers(workspace_code, agent_view_code)
    if not layers:
        return
    manifest = build_manifest(layers)
    apply_manifest(manifest, build_dir, strategy)


def _copy_module_workspaces(
    build_dir: Path,
    workspace_code: str,
    agent_view_code: str,
    *,
    strategy: Strategy = "copy",
) -> None:
    try:
        from agento.framework.bootstrap import get_manifests
        manifests = get_manifests()
    except Exception:
        return
    for manifest_entry in manifests:
        mod_workspace = Path(manifest_entry.path) / "workspace"
        if not mod_workspace.is_dir():
            continue
        dest = build_dir / "modules" / manifest_entry.name
        layers = _module_layers(mod_workspace, workspace_code, agent_view_code)
        manifest = build_manifest(layers)
        apply_manifest(manifest, dest, strategy)


def _write_skills_to_build(
    build_dir: Path,
    skills,
    registry,
    skills_dir: Path,
    *,
    strategy: Strategy = "copy",
) -> None:
    if not skills or registry is None:
        return
    output_dir = build_dir / ".claude" / "skills"
    output_dir.mkdir(parents=True, exist_ok=True)
    for skill in skills:
        source_dir: Path | None = None
        if skill.path:
            parent = Path(skill.path).parent
            if parent.is_dir():
                source_dir = parent
        if source_dir is None:
            candidate = skills_dir / skill.name
            if candidate.is_dir():
                source_dir = candidate
        if source_dir is None:
            logger.warning("Skill %r source directory not found — skipping", skill.name)
            continue
        manifest = {skill.name: (source_dir, "dir")}
        apply_manifest(manifest, output_dir, strategy)


def _create_agents_skills_symlink(build_dir: Path) -> None:
    """Create .agents/skills → ../.claude/skills symlink for Codex compatibility."""
    claude_skills = build_dir / ".claude" / "skills"
    if not claude_skills.is_dir():
        return
    agents_dir = build_dir / ".agents"
    agents_dir.mkdir(exist_ok=True)
    symlink = agents_dir / "skills"
    if symlink.is_symlink() or symlink.exists():
        symlink.unlink()
    symlink.symlink_to(Path("..") / ".claude" / "skills")


def _read_strategy(conn, source: str) -> Strategy:
    """Read workspace_build/strategy/{source} from global scope only, falling back to config.json."""
    if source not in _SOURCES:
        raise ValueError(f"Unknown workspace_build source: {source!r}")
    path = f"workspace_build/strategy/{source}"

    value: str | None = None
    if conn is not None:
        try:
            from agento.framework.scoped_config import Scope, load_scoped_db_overrides
            global_overrides = load_scoped_db_overrides(conn, Scope.DEFAULT, 0)
            entry = global_overrides.get(path)
            if entry is not None:
                value = entry[0]
        except Exception:
            logger.debug("Failed to read strategy/%s from DB; falling back to config.json", source)

    if value is None:
        try:
            from agento.framework.bootstrap import get_module_config
            cfg = get_module_config("workspace_build") or {}
            value = cfg.get(f"strategy/{source}")
        except Exception:
            value = None

    if value not in _STRATEGY_VALUES:
        if value is not None:
            logger.warning(
                "Invalid workspace_build strategy for %s: %r — falling back to 'copy'",
                source, value,
            )
        value = "copy"
    return value  # type: ignore[return-value]


def _resolve_strategies(conn) -> dict[str, Strategy]:
    return {source: _read_strategy(conn, source) for source in _SOURCES}


def ensure_state_dir(
    workspace_code: str,
    agent_view_code: str,
    persistent_paths: list[str],
) -> Path:
    return _ensure_state_dir(
        workspace_code, agent_view_code, persistent_paths,
        build_root=BUILD_DIR,
    )


def materialize_ssh_identity(
    build_dir: Path,
    resolved: dict[str, str],
) -> None:
    """Write SSH key, config, and known_hosts into ``build_dir/.ssh/`` when present.
    Values come pre-resolved (ENV->DB->config.json, already decrypted). Private key
    is written with mode 0600. Missing/empty fields are silently skipped.
    """
    private = resolved.get(_SSH_PRIVATE_KEY_PATH)
    public = resolved.get(_SSH_PUBLIC_KEY_PATH)
    config = resolved.get(_SSH_CONFIG_PATH)
    known_hosts = resolved.get(_SSH_KNOWN_HOSTS_PATH)

    if not (private or public or config or known_hosts):
        return

    ssh_dir = build_dir / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(ssh_dir, 0o700)

    if private:
        target = ssh_dir / "id_rsa"
        # Parse, do not eyeball. A structural check on the BEGIN/END envelope and a
        # plausible length lets through a key that is the right shape and the
        # wrong bytes — a paste that lost a middle line, a CRLF-mangled body — passes it
        # and still yields "Permission denied (publickey)". `derive_public_key` is a
        # single in-process parse against a key of a few hundred bytes, which is nothing
        # beside building a workspace, and it is the same call `identity:check` makes, so
        # the build warning and the CLI agree by construction.
        #
        # Last line of defence: config:set and the admin TUI both reject an unparsable
        # key now, but a row written before that guard existed (or by a direct DB edit)
        # would still land here and fail with nothing in the logs.
        try:
            derive_public_key(private)
        except EncryptedKeyError:
            logger.warning(
                "materialize_ssh_identity: %s is passphrase-protected; the agent "
                "cannot use it and git over SSH will fail. Store an unencrypted key.",
                _SSH_PRIVATE_KEY_PATH,
            )
        except ValueError:
            # Byte count only — never the key, and never the exception text, which
            # some backends echo key material into.
            logger.warning(
                "materialize_ssh_identity: %s does not parse as an SSH private key "
                "(%d bytes); git over SSH will fail. Run "
                "`agento agent_view:identity:check <code>`.",
                _SSH_PRIVATE_KEY_PATH, len(private),
            )
        target.write_text(private if private.endswith("\n") else private + "\n")
        os.chmod(target, 0o600)

    if public:
        (ssh_dir / "id_rsa.pub").write_text(public)

    if config:
        target = ssh_dir / "config"
        target.write_text(config)
        os.chmod(target, 0o600)

    if known_hosts:
        (ssh_dir / "known_hosts").write_text(known_hosts)


def materialize_git_identity(
    build_dir: Path,
    resolved: dict[str, str],
) -> None:
    """Write ``build_dir/.gitconfig`` ``[user]`` from agent_view/identity/git_author_*.
    Values come pre-resolved (ENV->DB->config.json) and are encoded safely (see
    ``git_identity.gitconfig_user_block``). Missing/empty fields are skipped; if neither is set,
    no file is written (behaves exactly as before this feature). The same values are ALSO exported
    as ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` on the agent at run time (which override even a
    repo-local ``.git/config``) — see ``agento.framework.git_identity.git_identity_env``."""
    block = gitconfig_user_block(
        resolved.get(GIT_AUTHOR_NAME_PATH) or "",
        resolved.get(GIT_AUTHOR_EMAIL_PATH) or "",
    )
    if block is None:
        return
    (build_dir / ".gitconfig").write_text(block)


def _read_retention_max_builds(conn) -> int:
    """Read workspace_build/retention/max_builds (global scope), default 10."""
    if conn is not None:
        try:
            from agento.framework.scoped_config import Scope, load_scoped_db_overrides
            global_overrides = load_scoped_db_overrides(conn, Scope.DEFAULT, 0)
            entry = global_overrides.get("workspace_build/retention/max_builds")
            if entry is not None and entry[0] is not None:
                return int(entry[0])
        except (ValueError, Exception):
            logger.debug("Failed to read retention/max_builds from DB; using config.json")

    try:
        from agento.framework.bootstrap import get_module_config
        cfg = get_module_config("workspace_build") or {}
        value = cfg.get("retention/max_builds", cfg.get("retention", {}).get("max_builds") if isinstance(cfg.get("retention"), dict) else None)
        if value is not None:
            return int(value)
    except Exception:
        pass
    return _DEFAULT_MAX_BUILDS


def materialize_agent_credentials(
    conn, build_dir: Path, *, harness: str | None, provider: str | None
) -> None:
    """Materialize the credential for THIS view's effective ``(harness, provider)`` only.

    Least privilege (AGENTS.md "Security"): a build is per agent_view, so iterating
    every registered harness — as this did — handed a Claude view the Codex credential
    merely because the ``codex`` module was enabled. Now exactly one credential scope
    is resolved, and a provider that requires no credential writes nothing at all.

    No enabled credential, or one missing the expected payload, is skipped silently —
    the agent will need to run ``/login`` on first use.
    """
    from agento.framework.agent_manager.credential_resolver import CredentialResolver
    from agento.framework.harness import resolve_provider, workspace_adapter_for

    if not harness or not provider:
        logger.debug("No effective harness/provider for this build; no credential written")
        return

    provider_desc = resolve_provider(harness, provider)
    if not provider_desc.credential_required:
        logger.debug(
            "Provider %s/%s requires no credential; nothing to materialize",
            harness, provider,
        )
        return

    scope = provider_desc.credential_scope
    try:
        credential = CredentialResolver().resolve(conn, scope)
    except Exception:
        logger.debug("No enabled credential for scope=%s; skipping", scope, exc_info=True)
        return
    if credential is None or credential.credentials is None:
        return

    _expiry = (credential.credentials.get("raw_auth") or {}).get("tokens", {}).get("expiry")
    if _expiry is not None:
        try:
            from datetime import UTC, datetime
            exp_dt = datetime.fromisoformat(str(_expiry).replace("Z", "+00:00"))
            if exp_dt < datetime.now(UTC):
                logger.warning(
                    "Credential for scope=%s is expired (expiry=%s); "
                    "re-authenticate via `agento credential:register %s <label>`",
                    scope, _expiry, scope,
                )
        except Exception:
            pass
    try:
        workspace_adapter_for(harness).write_credentials(build_dir, credential)
    except Exception:
        logger.warning(
            "WorkspaceAdapter for harness %s failed to write credentials",
            harness, exc_info=True,
        )


def migrate_legacy_workspace_config(writer, build_dir: Path) -> None:
    """Let the provider WorkspaceAdapter bridge legacy shared-HOME config into the build."""
    migrate = getattr(writer, "migrate_legacy_workspace_config", None)
    if migrate is None:
        return
    workspace_root = Path(BUILD_DIR).parent
    try:
        migrate(build_dir, workspace_root)
    except Exception:
        logger.warning(
            "WorkspaceAdapter %s failed to migrate legacy workspace config",
            writer.__class__.__name__,
            exc_info=True,
        )


def gc_old_builds(
    base_builds_dir: Path,
    current_build_id: int,
    max_builds: int,
    logger_: logging.Logger | None = None,
) -> list[int]:
    """Remove old build directories beyond ``max_builds`` (keeps the newest N
    including the current one). Returns the list of removed build ids.
    """
    _log = logger_ or logger
    if max_builds <= 0:
        return []
    if not base_builds_dir.is_dir():
        return []

    # Build IDs are numeric dir names; mtime-sort as fallback for non-numeric names.
    entries: list[tuple[int, Path]] = []
    for entry in base_builds_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            bid = int(entry.name)
        except ValueError:
            continue
        entries.append((bid, entry))

    entries.sort(key=lambda e: e[0], reverse=True)  # newest first
    to_keep = {bid for bid, _ in entries[:max_builds]}
    to_keep.add(current_build_id)

    removed = []
    for bid, path in entries:
        if bid in to_keep:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(bid)
        _log.info("Garbage-collected old build %d at %s", bid, path)
    return removed


def execute_build(conn, agent_view_id: int, *, force: bool = False) -> BuildResult:
    """Build a materialized workspace for an agent_view.

    When ``force=True``, bypass the "identical build already exists" skip check,
    delete any prior same-checksum build directory from disk, retire its DB row,
    and always produce a fresh ``build_id``. Useful when something outside the
    checksum inputs has changed (manual theme edits, external template updates).
    """
    from agento.framework.agent_view_runtime import resolve_agent_view_runtime
    from agento.framework.config_resolver import ScopedConfigService
    from agento.framework.harness import (
        get_agent_config,
        get_harness,
        get_harness_config,
        persistent_home_paths_for,
        supply_harness_config,
        workspace_adapter_for,
    )
    from agento.framework.scoped_config import Scope
    from agento.framework.workspace import get_agent_view

    agent_view = get_agent_view(conn, agent_view_id)
    if agent_view is None:
        raise ValueError(f"agent_view {agent_view_id} not found")

    with conn.cursor() as cur:
        cur.execute("SELECT code FROM workspace WHERE id = %s", (agent_view.workspace_id,))
        ws_row = cur.fetchone()
    if ws_row is None:
        raise ValueError(f"workspace {agent_view.workspace_id} not found")
    workspace_code = ws_row["code"] if isinstance(ws_row, dict) else ws_row[0]

    # Single config service for this agent_view. The fully-resolved effective
    # config (ENV -> DB -> config.json, incl. ENV-only fields, decrypted) drives
    # both the freshness checksum and every materialized file — one view, no split.
    svc = ScopedConfigService(
        conn, Scope.AGENT_VIEW, agent_view_id, workspace_id=agent_view.workspace_id,
    )
    resolved = svc.resolve_all()

    # Fetch enabled skills once (used for checksum + build)
    enabled_skills, skill_registry = _get_enabled_skills(
        conn, agent_view_id, agent_view.workspace_id,
    )
    skill_checksums = [s.checksum for s in enabled_skills]

    # Resolve per-source strategies (global-scope only).
    strategies = _resolve_strategies(conn)

    checksum = compute_build_checksum(
        resolved, skill_checksums, strategies=strategies,
    )

    # Skip if identical build already exists AND its build_dir is intact on disk.
    # When force=True, look up the prior build to clean it up, then always rebuild.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, build_dir FROM workspace_build "
            "WHERE agent_view_id = %s AND checksum = %s AND status = 'ready'",
            (agent_view_id, checksum),
        )
        existing = cur.fetchone()
    if not force and existing and existing["build_dir"] and Path(existing["build_dir"]).is_dir():
        logger.info(
            "Build %d already exists with checksum %s, skipping",
            existing["id"], checksum[:12],
        )
        existing_build_dir = Path(existing["build_dir"])
        current_link = existing_build_dir.parent.parent / "current"
        if not current_link.is_symlink() or current_link.resolve() != existing_build_dir.resolve():
            if current_link.is_symlink() or current_link.exists():
                current_link.unlink()
            current_link.symlink_to(existing_build_dir)
        result = BuildResult(
            build_id=existing["id"],
            build_dir=existing["build_dir"],
            checksum=checksum,
            skipped=True,
        )
        _dispatch("workspace_build_complete_after", WorkspaceBuildCompletedEvent(
            agent_view_id=agent_view_id, build_id=existing["id"],
            build_dir=existing["build_dir"], checksum=checksum, skipped=True,
        ))
        return result
    if existing:
        # Either disk is gone (stale record) or force=True. Retire the old record
        # and clean up any on-disk remnants so the new build owns the checksum.
        if force:
            logger.info(
                "Force rebuild: retiring prior build %d (checksum %s) and cleaning %s",
                existing["id"], checksum[:12], existing["build_dir"],
            )
        else:
            logger.warning(
                "Build %d marked ready but build_dir %s is missing — rebuilding",
                existing["id"], existing["build_dir"],
            )
        if existing["build_dir"]:
            shutil.rmtree(existing["build_dir"], ignore_errors=True)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workspace_build SET status = 'failed' WHERE id = %s",
                (existing["id"],),
            )
        conn.commit()

    # Insert new build record
    base = Path(BUILD_DIR) / workspace_code / agent_view.code / "builds"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workspace_build (agent_view_id, build_dir, checksum, status) "
            "VALUES (%s, %s, %s, 'building')",
            (agent_view_id, "", checksum),
        )
        build_id = cur.lastrowid
    conn.commit()

    _dispatch("workspace_build_start_after", WorkspaceBuildStartedEvent(
        agent_view_id=agent_view_id, build_id=build_id,
    ))

    build_dir = base / str(build_id)

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workspace_build SET build_dir = %s WHERE id = %s",
            (str(build_dir), build_id),
        )
    conn.commit()

    try:
        if build_dir.exists():
            # lastrowid collisions shouldn't happen, but guarantee a clean dest dir.
            shutil.rmtree(build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)

        # 1. Theme as base layer (scaffolding, KnowledgeBase, etc.)
        _copy_theme(build_dir, workspace_code, agent_view.code, strategy=strategies["theme"])

        # 2. Agent CLI configs via provider-specific WorkspaceAdapter
        runtime = resolve_agent_view_runtime(conn, agent_view_id)
        if runtime.provider:
            agent_config = get_agent_config(svc)
            core_cfg = ScopedConfigService(conn).get_module("core") or {}
            toolbox_url = core_cfg.get("toolbox/url") or "http://toolbox:3001"
            writer = workspace_adapter_for(runtime.harness)
            kwargs = supply_harness_config(
                writer,
                {"agent_view_id": agent_view_id, "toolbox_url": toolbox_url},
                get_harness_config(svc, get_harness(runtime.harness)),
            )
            writer.prepare_workspace(build_dir, agent_config, **kwargs)
            migrate_legacy_workspace_config(writer, build_dir)

        # 3. Instruction files (AGENTS.md, SOUL.md, CLAUDE.md)
        _write_instruction_files(build_dir, resolved)

        # 4. Module workspace assets (namespaced under modules/{name}/)
        _copy_module_workspaces(
            build_dir, workspace_code, agent_view.code, strategy=strategies["modules"],
        )

        # 5. Skills (soft dependency)
        skills_dir = _resolve_skills_dir()
        _write_skills_to_build(
            build_dir, enabled_skills, skill_registry, skills_dir,
            strategy=strategies["skills"],
        )

        # 6. .agents/skills symlink → .claude/skills (Codex compatibility)
        _create_agents_skills_symlink(build_dir)

        # 7. SSH identity (private key, pub, config, known_hosts) from DB
        materialize_ssh_identity(build_dir, resolved)

        # 7b. Git commit author identity (~/.gitconfig [user]) from DB — so the agent's commits
        # are authored correctly (e.g. linked to the Bitbucket account by verified email).
        materialize_git_identity(build_dir, resolved)

        # 8. Persistent-state symlinks for harness-declared paths (session dirs, etc.).
        # Scoped to runtime.harness — prevents cross-harness state leaking into the build.
        # A view with no harness configured owns no such paths.
        persistent_paths = (
            persistent_home_paths_for(runtime.harness) if runtime.harness else []
        )
        state_dir = ensure_state_dir(workspace_code, agent_view.code, persistent_paths)
        link_persistent_paths(build_dir, state_dir, persistent_paths)

        # 9. Materialize agent OAuth credentials files per registered WorkspaceAdapter
        materialize_agent_credentials(
            conn, build_dir, harness=runtime.harness, provider=runtime.provider
        )

        # Mark as ready
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workspace_build SET status = 'ready' WHERE id = %s",
                (build_id,),
            )
        conn.commit()

        # Update 'current' symlink
        current_link = base.parent / "current"
        if current_link.is_symlink() or current_link.exists():
            current_link.unlink()
        current_link.symlink_to(build_dir)

        # 10. Retention: prune old builds
        max_builds = _read_retention_max_builds(conn)
        gc_old_builds(base, current_build_id=build_id, max_builds=max_builds)

        logger.info("Build %d ready at %s (checksum %s)", build_id, build_dir, checksum[:12])
        _dispatch("workspace_build_complete_after", WorkspaceBuildCompletedEvent(
            agent_view_id=agent_view_id, build_id=build_id,
            build_dir=str(build_dir), checksum=checksum,
        ))
        return BuildResult(build_id=build_id, build_dir=str(build_dir), checksum=checksum)

    except Exception as exc:
        _dispatch("workspace_build_fail_after", WorkspaceBuildFailedEvent(
            agent_view_id=agent_view_id, build_id=build_id, error=str(exc),
        ))
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workspace_build SET status = 'failed' WHERE id = %s",
                (build_id,),
            )
        conn.commit()
        raise
