from pathlib import Path

import pytest

from code_runner.skills import SkillLoader, SkillSpec, SkillsNamespace


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


def test_namespace_calls_skill_function(tmp_path):
    loader = SkillLoader(FIXTURE_DIR)
    ns = SkillsNamespace(loader.discover())
    out_path = tmp_path / "out.csv"
    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    written = ns.sample_csv.write_csv(rows, str(out_path))
    assert written == 2
    text = out_path.read_text()
    assert "a,b" in text and "1,x" in text


def test_namespace_unknown_skill_raises():
    ns = SkillsNamespace({})
    with pytest.raises(AttributeError, match="unknown"):
        ns.unknown


def test_namespace_unknown_function_raises():
    loader = SkillLoader(FIXTURE_DIR)
    ns = SkillsNamespace(loader.discover())
    with pytest.raises(AttributeError):
        ns.sample_csv.does_not_exist


def test_namespace_hides_private_names():
    loader = SkillLoader(FIXTURE_DIR)
    ns = SkillsNamespace(loader.discover())
    with pytest.raises(AttributeError):
        ns.sample_csv._private


def test_namespace_repr_lists_skills():
    loader = SkillLoader(FIXTURE_DIR)
    ns = SkillsNamespace(loader.discover())
    r = repr(ns)
    assert "sample_csv" in r


def test_broken_skill_raises_only_on_access(tmp_path):
    skill_dir = tmp_path / "broken"
    skill_dir.mkdir()
    (skill_dir / "script.py").write_text("def f(:\n    pass\n")  # SyntaxError
    (skill_dir / "SKILL.md").write_text("---\ndescription: broken\n---")
    ns = SkillsNamespace(SkillLoader(tmp_path).discover())
    # Construction did not raise
    with pytest.raises(RuntimeError, match="broken"):
        ns.broken.f
