"""Every core-module skill loads in every harness, and the miniapp build skill names real tools.

Codex refuses a SKILL.md without YAML frontmatter (`missing YAML frontmatter delimited by ---`),
so a module skill without `name` and `description` is invisible to a Codex agent.
"""
import json
import re
from pathlib import Path

import agento.modules
from agento.modules.skill.src.registry import _frontmatter_description

MODULES = Path(next(iter(agento.modules.__path__)))
SKILLS = sorted(MODULES.glob("*/skills/*/SKILL.md"))


def _frontmatter_name(lines: list[str]) -> str | None:
    body_start, _ = _frontmatter_description(lines)
    for line in lines[1:body_start]:
        key, _, value = line.partition(":")
        if key.strip() == "name":
            return value.strip()
    return None


def test_module_skills_are_discovered():
    assert any(p.parent.name == "miniapp-build" for p in SKILLS)


def test_every_module_skill_has_frontmatter_name_and_description():
    bad = []
    for path in SKILLS:
        lines = path.read_text().splitlines()
        _, description = _frontmatter_description(lines)
        if _frontmatter_name(lines) != path.parent.name or not description:
            bad.append(str(path.relative_to(MODULES)))
    assert bad == []


def test_miniapp_build_names_only_declared_tools():
    declared = {
        tool["name"]
        for module in ("versioned_artifacts", "miniapps")
        for tool in json.loads((MODULES / module / "module.json").read_text())["tools"]
    }
    text = (MODULES / "miniapps/skills/miniapp-build/SKILL.md").read_text()
    named = set(re.findall(r"`((?:versioned_artifact|miniapp)_[a-z_]+)`", text))
    assert named, "the skill names no tool: the pattern no longer matches"
    assert named - declared == set()
