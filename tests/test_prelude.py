"""Tests for the auto-prelude alias layer."""

from pathlib import Path

from code_runner import prelude
from code_runner.skills import SkillLoader, SkillsNamespace


def _write_skill(root: Path, name: str, source: str) -> None:
    d = root / name
    d.mkdir()
    (d / "script.py").write_text(source)
    (d / "SKILL.md").write_text("---\ndescription: test\n---\n")


def _load(root: Path) -> SkillsNamespace:
    return SkillsNamespace(SkillLoader(root).discover())


def test_build_returns_empty_when_no_skills():
    assert prelude.build(None) == {}


def test_build_aliases_present_skills(tmp_path):
    _write_skill(
        tmp_path, "forgetful_compact",
        "async def query(query_text, client):\n    return (query_text, client)\n",
    )
    ns = _load(tmp_path)
    aliases = prelude.build(ns)

    assert "fg_query" in aliases
    assert callable(aliases["fg_query"])
    assert aliases["fg_query"] is ns.forgetful_compact.query


def test_build_skips_missing_skills(tmp_path):
    ns = _load(tmp_path)
    assert prelude.build(ns) == {}


def test_build_skips_missing_function_in_skill(tmp_path):
    _write_skill(
        tmp_path, "forgetful_compact",
        "def something_else():\n    return 1\n",
    )
    ns = _load(tmp_path)
    aliases = prelude.build(ns)
    assert "fg_query" not in aliases


def test_build_survives_broken_skill(tmp_path):
    # Syntax error in script — skill becomes a _BrokenSkill placeholder.
    # prelude.build must not crash trying to access it.
    _write_skill(tmp_path, "forgetful_compact", "def query(:\n    bad syntax\n")
    ns = _load(tmp_path)
    aliases = prelude.build(ns)
    assert "fg_query" not in aliases


def test_describe_returns_documented_aliases():
    described = prelude.describe()
    names = [alias for alias, _ in described]

    assert "fg_query" in names
    assert "md_table" in names
    assert "probe" in names

    for _, path in described:
        assert path.startswith("skills.")
        assert path.count(".") == 2


def test_no_duplicate_aliases():
    seen = set()
    for alias, _, _ in prelude.ALIASES:
        assert alias not in seen, f"duplicate alias: {alias}"
        seen.add(alias)
