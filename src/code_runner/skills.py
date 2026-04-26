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


import builtins as _builtins
from typing import Any


class SkillProxy:
    """Thin wrapper exposing a single skill's public callables.

    Underscore-prefixed names (including imported modules used inside the
    skill) are hidden so they don't pollute `dir(skills.foo)` or shadow
    the framework namespace.
    """

    __slots__ = ("_name", "_callables")

    def __init__(self, name: str, callables: dict[str, Any]) -> None:
        self._name = name
        self._callables = callables

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr not in self._callables:
            raise AttributeError(
                f"skill {self._name!r} has no callable {attr!r}"
            )
        return self._callables[attr]

    def __dir__(self):
        return list(self._callables)

    def __repr__(self):
        fns = ", ".join(sorted(self._callables))
        return f"<SkillProxy {self._name} fns=[{fns}]>"


class _BrokenSkill:
    """Surfaces a skill load error only when accessed, not at startup.

    A single broken skill must not crash the executor — it just becomes
    unreachable. Accessing any attribute raises with the original error.
    """

    __slots__ = ("_name", "_error")

    def __init__(self, name: str, error: Exception) -> None:
        self._name = name
        self._error = error

    def __getattr__(self, attr: str):
        raise RuntimeError(
            f"skill {self._name!r} failed to load: {self._error}"
        )


class SkillsNamespace:
    """Attribute-accessible bag of skills for sandbox injection.

    Each spec's source is exec'd ONCE at construction time in an isolated
    module dict with full builtins (skills are trusted local code; we
    don't run AST validation on them). Public callables become attributes
    on the per-skill SkillProxy.
    """

    __slots__ = ("_proxies",)

    def __init__(self, specs: dict[str, SkillSpec]) -> None:
        self._proxies: dict[str, Any] = {}
        for name, spec in specs.items():
            module_ns: dict[str, Any] = {"__builtins__": _builtins.__dict__}
            try:
                code = compile(
                    spec.source, str(spec.path / "script.py"), "exec"
                )
                exec(code, module_ns)
            except Exception as e:
                self._proxies[name] = _BrokenSkill(name, e)
                continue
            callables = {
                k: v for k, v in module_ns.items()
                if callable(v) and not k.startswith("_")
            }
            self._proxies[name] = SkillProxy(name, callables)

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr not in self._proxies:
            raise AttributeError(f"unknown skill: {attr}")
        return self._proxies[attr]

    def __dir__(self):
        return list(self._proxies)

    def __repr__(self):
        names = ", ".join(sorted(self._proxies))
        return f"<Skills [{names}]>"
