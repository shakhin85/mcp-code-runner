"""SHA-500: a failed skill_call event carries `error` = '<Class>: <first line>' (<=200)."""

import asyncio
import sys

import pytest

from code_runner.executor import CodeExecutor
from code_runner.skills import SkillLoader, SkillsNamespace

ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]


class _Recorder:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


class _Pool:
    sessions = {}
    tools = {}


def _run(tmp_path, isolation, code, monkeypatch):
    monkeypatch.setenv("CODE_RUNNER_START_METHOD", "spawn")
    d = tmp_path / "skills" / "calc"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("calc\n", encoding="utf-8")
    (d / "script.py").write_text(
        "def ok():\n    return 1\n\n"
        "def multi():\n    raise ValueError('first\\nsecond\\nthird')\n\n"
        "def long():\n    raise RuntimeError('x' * 300)\n",
        encoding="utf-8",
    )
    skills = SkillsNamespace(SkillLoader(tmp_path / "skills").discover())
    recorder = _Recorder()
    ex = CodeExecutor(_Pool(), skills=skills, recorder=recorder, isolation=isolation,
                      mem_limit_mb=1024)
    result = asyncio.run(ex.execute(code, timeout=10))
    return result, [e for e in recorder.events if e["kind"] == "skill_call"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_multiline_error_keeps_first_line_and_exception_propagates(
    isolation, tmp_path, monkeypatch
):
    result, events = _run(
        tmp_path, isolation,
        "try:\n    skills.calc.multi()\nexcept ValueError as e:\n    print('caught', e)\n",
        monkeypatch,
    )
    assert result["success"], result["error"]
    assert "caught first" in result["output"]
    assert events[0]["success"] is False
    assert events[0]["error"] == "ValueError: first"


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_long_error_is_capped_at_200(isolation, tmp_path, monkeypatch):
    result, events = _run(tmp_path, isolation, "skills.calc.long()", monkeypatch)
    assert "RuntimeError" in result["error"]
    error = events[0]["error"]
    assert len(error) <= 200
    assert error.startswith("RuntimeError: xxx")


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_successful_call_has_no_error_key(isolation, tmp_path, monkeypatch):
    result, events = _run(tmp_path, isolation, "skills.calc.ok()", monkeypatch)
    assert result["success"], result["error"]
    assert events[0]["success"] is True
    assert "error" not in events[0]
