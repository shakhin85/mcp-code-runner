"""
Discover and expose skills from ~/.claude/code-runner-skills/<name>/.

A skill is a directory with two files:
  - script.py: Python source (trusted local code, full builtins)
  - SKILL.md: human description; optional YAML-ish frontmatter

The loader returns a catalog of SkillSpec objects. Wiring into the
executor and the namespace proxy is handled in Task 5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillSpec:
    name: str
    source: str
    description: str
    path: Path


_FRONTMATTER_DESC_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)


def _parse_description(md_text: str) -> str:
    text = md_text.strip()
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            front = text[3:end]
            m = _FRONTMATTER_DESC_RE.search(front)
            if m:
                return m.group(1)
            text = text[end + 3:].strip()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


class SkillLoader:
    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = Path(skills_dir)

    def discover(self) -> dict[str, SkillSpec]:
        if not self.skills_dir.is_dir():
            return {}
        out: dict[str, SkillSpec] = {}
        for child in sorted(self.skills_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            script = child / "script.py"
            skill_md = child / "SKILL.md"
            if not script.is_file() or not skill_md.is_file():
                continue
            source = script.read_text(encoding="utf-8")
            description = _parse_description(
                skill_md.read_text(encoding="utf-8")
            )
            out[child.name] = SkillSpec(
                name=child.name,
                source=source,
                description=description,
                path=child,
            )
        return out
