from pathlib import Path

from code_runner.server import _format_skills_section
from code_runner.skills import SkillSpec


def _spec(name: str, description: str) -> SkillSpec:
    return SkillSpec(
        name=name, source="", description=description, path=Path("/tmp"),
    )


def test_skills_section_lists_each_skill():
    specs = {
        "csv_export": _spec("csv_export", "Write rows to CSV."),
        "snapshot_diff": _spec("snapshot_diff", "Diff two row lists."),
    }
    out = _format_skills_section(specs)
    assert "# === Skills ===" in out
    assert "# - skills.csv_export: Write rows to CSV." in out
    assert "# - skills.snapshot_diff: Diff two row lists." in out


def test_skills_section_empty_when_no_skills():
    assert _format_skills_section({}) == ""


def test_skills_section_sorted_by_name():
    specs = {
        "zeta": _spec("zeta", "Z"),
        "alpha": _spec("alpha", "A"),
    }
    out = _format_skills_section(specs)
    alpha_pos = out.index("alpha")
    zeta_pos = out.index("zeta")
    assert alpha_pos < zeta_pos


def test_skills_section_handles_blank_description():
    specs = {"naked": _spec("naked", "")}
    out = _format_skills_section(specs)
    # No trailing colon for blank description
    assert "# - skills.naked" in out
    assert "# - skills.naked:" not in out
