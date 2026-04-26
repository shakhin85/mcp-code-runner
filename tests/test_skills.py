from pathlib import Path

import pytest

from code_runner.skills import SkillLoader, SkillSpec


FIXTURE_DIR = Path(__file__).parent / "skills_fixtures"


def test_discover_finds_sample_csv():
    loader = SkillLoader(FIXTURE_DIR)
    skills = loader.discover()
    assert "sample_csv" in skills
    spec = skills["sample_csv"]
    assert isinstance(spec, SkillSpec)
    assert spec.name == "sample_csv"
    assert "list-of-dicts" in spec.description.lower()
    assert "def write_csv" in spec.source


def test_discover_skips_skill_without_md():
    loader = SkillLoader(FIXTURE_DIR)
    skills = loader.discover()
    assert "no_md" not in skills


def test_discover_returns_empty_when_dir_missing(tmp_path):
    loader = SkillLoader(tmp_path / "does-not-exist")
    assert loader.discover() == {}


def test_discover_ignores_hidden_dirs(tmp_path):
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "script.py").write_text("x = 1")
    (tmp_path / ".hidden" / "SKILL.md").write_text("---\ndescription: x\n---")
    loader = SkillLoader(tmp_path)
    assert loader.discover() == {}


def test_description_falls_back_to_first_line_when_no_frontmatter(tmp_path):
    skill_dir = tmp_path / "plain"
    skill_dir.mkdir()
    (skill_dir / "script.py").write_text("def f(): pass")
    (skill_dir / "SKILL.md").write_text("Just a paragraph describing it.\n\nMore details.")
    loader = SkillLoader(tmp_path)
    spec = loader.discover()["plain"]
    assert spec.description == "Just a paragraph describing it."
