"""Skill registry — scan from disk, sync to DB, query enabled skills."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SkillInfo:
    name: str
    path: str
    description: str
    checksum: str


@dataclass
class SyncResult:
    new: int
    updated: int
    unchanged: int


def _frontmatter_description(lines: list[str]) -> tuple[int, str | None]:
    """Return (index of the first body line, frontmatter `description` or None).

    Reads only a leading `---` block: `description: text`, or a `>` / `|` block whose
    indented lines are joined with one space. No frontmatter -> (0, None).
    """
    if not lines or lines[0].strip() != "---":
        return 0, None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return 0, None
    for i in range(1, end):
        key, sep, value = lines[i].partition(":")
        if not sep or key.strip() != "description":
            continue
        value = value.strip()
        if value[:1] in (">", "|"):
            folded = []
            for nxt in lines[i + 1:end]:
                if nxt and not nxt[0].isspace():
                    break
                folded.append(nxt.strip())
            value = " ".join(p for p in folded if p)
        return end + 1, value.strip("'\"")[:500] or None
    return end + 1, None


def scan_skills(skills_dir: Path) -> list[SkillInfo]:
    """Scan disk for skill directories containing SKILL.md."""
    if not skills_dir.is_dir():
        return []
    skills = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            continue
        content = skill_file.read_text()
        checksum = hashlib.sha256(content.encode()).hexdigest()
        lines = content.splitlines()
        body_start, description = _frontmatter_description(lines)
        if description is None:
            # Description: first non-empty body line after optional # heading
            description = ""
            for line in lines[body_start:]:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    description = stripped[:500]
                    break
        skills.append(SkillInfo(
            name=entry.name,
            path=str(skill_file),
            description=description,
            checksum=checksum,
        ))
    return skills


def scan_skills_multi(skills_dirs: list[Path]) -> list[SkillInfo]:
    """Scan multiple directories for skills. First occurrence wins on name collision."""
    seen: set[str] = set()
    result: list[SkillInfo] = []
    for sdir in skills_dirs:
        for skill in scan_skills(sdir):
            if skill.name in seen:
                logger.warning(
                    "Skill name collision: '%s' from %s (skipping, already registered from earlier source)",
                    skill.name, sdir,
                )
                continue
            seen.add(skill.name)
            result.append(skill)
    return result


def sync_skills_multi(conn, skills_dirs: list[Path]) -> SyncResult:
    """Sync skills from multiple source directories. First occurrence wins."""
    scanned = scan_skills_multi(skills_dirs)
    result = _upsert_skills(conn, scanned)
    _dispatch_sync_event(str(skills_dirs), result)
    return result


def sync_skills(conn, skills_dir: Path) -> SyncResult:
    """Upsert scanned skills into skill_registry."""
    scanned = scan_skills(skills_dir)
    result = _upsert_skills(conn, scanned)
    _dispatch_sync_event(str(skills_dir), result)
    return result


def _upsert_skills(conn, scanned: list[SkillInfo]) -> SyncResult:
    """Upsert scanned skills into skill_registry (no event dispatch)."""
    new = updated = unchanged = 0

    with conn.cursor() as cur:
        for skill in scanned:
            cur.execute("SELECT id, checksum, description FROM skill_registry WHERE name = %s", (skill.name,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO skill_registry (name, path, description, checksum, synced_at) "
                    "VALUES (%s, %s, %s, %s, NOW())",
                    (skill.name, skill.path, skill.description, skill.checksum),
                )
                new += 1
            else:
                existing_checksum = row["checksum"] if isinstance(row, dict) else row[1]
                existing_description = row.get("description") if isinstance(row, dict) else row[2]
                # The description is derived, so a parser change can move it without a file change.
                if existing_checksum != skill.checksum or existing_description != skill.description:
                    cur.execute(
                        "UPDATE skill_registry SET path=%s, description=%s, checksum=%s, synced_at=NOW() "
                        "WHERE name=%s",
                        (skill.path, skill.description, skill.checksum, skill.name),
                    )
                    updated += 1
                else:
                    unchanged += 1
    conn.commit()
    return SyncResult(new=new, updated=updated, unchanged=unchanged)


def _dispatch_sync_event(skills_dir_str: str, result: SyncResult) -> None:
    try:
        from agento.framework.event_manager import get_event_manager
        from agento.framework.events import SkillSyncCompletedEvent
        get_event_manager().dispatch("skill_sync_complete_after", SkillSyncCompletedEvent(
            skills_dir=skills_dir_str, new=result.new, updated=result.updated, unchanged=result.unchanged,
        ))
    except Exception:
        pass


def get_all_skills(conn) -> list[SkillInfo]:
    """Get all registered skills from DB."""
    with conn.cursor() as cur:
        cur.execute("SELECT name, path, description, checksum FROM skill_registry ORDER BY name")
        rows = cur.fetchall()
    result = []
    for row in rows:
        if isinstance(row, dict):
            result.append(SkillInfo(name=row["name"], path=row["path"], description=row["description"], checksum=row["checksum"]))
        else:
            result.append(SkillInfo(name=row[0], path=row[1], description=row[2], checksum=row[3]))
    return result


def get_enabled_skills(conn, agent_view_id: int | None = None, workspace_id: int | None = None) -> list[SkillInfo]:
    """Get skills that are enabled for the given scope.

    Opt-in: a skill is enabled only when its resolved value is explicitly '1'.
    Missing (no row) and explicit '0' both mean disabled. Resolution goes through
    the one config service (ScopedConfigService) as the source of truth for the
    merged agent_view > workspace > default scope chain. Skill names may contain
    dashes (e.g. ``git-workflow``); ``.get()`` path-normalizes dashes, so we read
    the service's merged ``.overrides`` to stay dash-exact (skills carry no
    config.json/ENV is_enabled defaults, so the DB scope chain is the full story).
    """
    from agento.framework.config_resolver import ScopedConfigService
    from agento.framework.scoped_config import Scope

    all_skills = get_all_skills(conn)
    svc = _scoped_config(conn, agent_view_id, workspace_id, ScopedConfigService, Scope)

    enabled = []
    for skill in all_skills:
        entry = svc.overrides.get(f"skill/{skill.name}/is_enabled")
        if entry is None or entry[0] != "1":
            continue
        enabled.append(skill)
    return enabled


def _scoped_config(conn, agent_view_id, workspace_id, service_cls, scope_cls):
    """Build the config service for a (agent_view_id, workspace_id) pair."""
    if agent_view_id is not None:
        return service_cls(conn, scope_cls.AGENT_VIEW, agent_view_id, workspace_id=workspace_id)
    if workspace_id is not None:
        return service_cls(conn, scope_cls.WORKSPACE, workspace_id)
    return service_cls(conn, scope_cls.DEFAULT, 0)


def get_skill_content(name: str, skills_dir: Path, path: str | None = None) -> str | None:
    """Read SKILL.md content from disk."""
    # Registered path takes priority — handles module skills with absolute paths
    if path:
        registered = Path(path)
        if registered.is_file():
            return registered.read_text()
    # Fallback: user workspace skills layout
    skill_file = skills_dir / name / "SKILL.md"
    if skill_file.is_file():
        return skill_file.read_text()
    return None
