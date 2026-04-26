import pytest
from pathlib import Path

from code_runner.skills import (
    SkillLoader,
    SkillsNamespace,
    write_skill_files,
    validate_skill_name,
)


def test_validate_skill_name_accepts_good():
    validate_skill_name("foo")
    validate_skill_name("foo_bar_2")
    validate_skill_name("a")


def test_validate_skill_name_rejects_bad():
    bad_names = [
        "",
        "Foo",       # uppercase
        "1foo",      # leading digit
        "foo-bar",   # hyphen
        "foo bar",   # space
        "x" * 41,    # too long
        "foo.bar",   # dot
        "_foo",      # leading underscore
    ]
    for bad in bad_names:
        with pytest.raises(ValueError):
            validate_skill_name(bad)


def test_write_skill_files_creates_both(tmp_path):
    target = write_skill_files(tmp_path, "demo", "def f(): return 1", "demo skill")
    assert (target / "script.py").read_text() == "def f(): return 1"
    md = (target / "SKILL.md").read_text()
    assert "name: demo" in md
    assert "description: demo skill" in md
    assert md.startswith("---\n") and md.rstrip().endswith("---")


def test_write_then_discover_then_call(tmp_path):
    write_skill_files(tmp_path, "demo", "def f(): return 42", "the answer")
    specs = SkillLoader(tmp_path).discover()
    assert "demo" in specs
    ns = SkillsNamespace(specs)
    assert ns.demo.f() == 42


def test_overwrite_existing_skill(tmp_path):
    write_skill_files(tmp_path, "demo", "def f(): return 1", "v1")
    write_skill_files(tmp_path, "demo", "def f(): return 2", "v2")
    ns = SkillsNamespace(SkillLoader(tmp_path).discover())
    assert ns.demo.f() == 2
