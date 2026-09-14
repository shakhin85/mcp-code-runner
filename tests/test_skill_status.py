import asyncio
import json
from pathlib import Path

import pytest

from code_runner.skill_status import demote, record_outcome
from code_runner.skills import SkillLoader, SkillsNamespace


def _status(skill_dir: Path) -> dict:
    return json.loads((skill_dir / "status.json").read_text(encoding="utf-8"))


def _make_skill(root: Path, source: str, status: dict | None) -> Path:
    skill_dir = root / "probe"
    skill_dir.mkdir()
    (skill_dir / "script.py").write_text(source)
    (skill_dir / "SKILL.md").write_text("---\ndescription: probe\n---")
    if status is not None:
        (skill_dir / "status.json").write_text(json.dumps(status))
    return skill_dir


def _skills(root: Path) -> SkillsNamespace:
    return SkillsNamespace(SkillLoader(root).discover())


SOURCE = """
def ok():
    return 1

def boom():
    raise ValueError("boom")

async def aok():
    return 2

async def aboom():
    raise ValueError("aboom")
"""


@pytest.mark.parametrize(
    ("status", "uses", "successes", "expected"),
    [
        ("VERIFIED", 4, 0, "VERIFIED"),   # below MIN_USES_FOR_DEMOTION
        ("VERIFIED", 10, 9, "VERIFIED"),  # rate 0.9 >= threshold
        ("VERIFIED", 5, 3, "TRIAL"),      # rate 0.6 on 5 uses
        ("VERIFIED", 5, 4, "VERIFIED"),   # rate exactly 0.8
        ("TRIAL", 10, 0, "TRIAL"),        # TRIAL never changes here
    ],
)
def test_demote_boundaries(status, uses, successes, expected):
    result = demote({"status": status, "uses": uses, "successes": successes})
    assert result["status"] == expected


def test_record_outcome_keeps_other_fields(tmp_path):
    (tmp_path / "status.json").write_text(
        json.dumps({"status": "TRIAL", "uses": 0, "successes": 0, "note": "x"})
    )
    record_outcome(tmp_path, ok=True)
    record_outcome(tmp_path, ok=False)
    assert _status(tmp_path) == {
        "status": "TRIAL", "uses": 2, "successes": 1, "note": "x"
    }


def test_record_outcome_skips_unenrolled_skill(tmp_path):
    record_outcome(tmp_path, ok=True)
    assert not (tmp_path / "status.json").exists()


def test_proxy_counts_sync_success_and_failure(tmp_path):
    skill_dir = _make_skill(
        tmp_path, SOURCE, {"status": "TRIAL", "uses": 0, "successes": 0}
    )
    skills = _skills(tmp_path)
    assert skills.probe.ok() == 1
    with pytest.raises(ValueError, match="boom"):
        skills.probe.boom()
    assert _status(skill_dir) == {"status": "TRIAL", "uses": 2, "successes": 1}


def test_proxy_counts_async_after_await(tmp_path):
    skill_dir = _make_skill(
        tmp_path, SOURCE, {"status": "TRIAL", "uses": 0, "successes": 0}
    )
    skills = _skills(tmp_path)
    coro = skills.probe.aok()
    assert _status(skill_dir)["uses"] == 0
    assert asyncio.run(coro) == 2
    with pytest.raises(ValueError, match="aboom"):
        asyncio.run(skills.probe.aboom())
    assert _status(skill_dir) == {"status": "TRIAL", "uses": 2, "successes": 1}


def test_proxy_demotes_verified_skill_on_low_success_rate(tmp_path):
    skill_dir = _make_skill(
        tmp_path, SOURCE, {"status": "VERIFIED", "uses": 3, "successes": 3}
    )
    skills = _skills(tmp_path)
    for _ in range(2):
        with pytest.raises(ValueError):
            skills.probe.boom()
    assert _status(skill_dir) == {"status": "TRIAL", "uses": 5, "successes": 3}


def test_proxy_without_status_json_writes_nothing(tmp_path):
    skill_dir = _make_skill(tmp_path, SOURCE, None)
    assert _skills(tmp_path).probe.ok() == 1
    assert sorted(p.name for p in skill_dir.iterdir()) == ["SKILL.md", "script.py"]
