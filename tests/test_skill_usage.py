"""SHA-484: per-function skill usage in metrics and the trim report.

`save_skill` only grows the skill set. Each `skills.<name>.<fn>` call now lands
as a `skill_call` event; `summarize_skills` turns those into a table with
removal candidates (unused / failing) for a human to decide on.
"""

import asyncio
import sys

import pytest

from code_runner.executor import CodeExecutor
from code_runner.metrics import (
    SKILL_TRIM_ERROR_SHARE,
    SKILL_TRIM_MIN_CALLS,
    summarize_skills,
)
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


def _skills(tmp_path):
    d = tmp_path / "skills" / "calc"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("calc\n", encoding="utf-8")
    (d / "script.py").write_text(
        "def ok(x):\n    return x + 1\n\n"
        "def boom():\n    raise ValueError('nope')\n\n"
        "def idle():\n    return None\n",
        encoding="utf-8",
    )
    return SkillsNamespace(SkillLoader(tmp_path / "skills").discover())


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_skill_calls_recorded_per_function(tmp_path, isolation):
    recorder = _Recorder()
    ex = CodeExecutor(
        _Pool(), skills=_skills(tmp_path), recorder=recorder,
        isolation=isolation, mem_limit_mb=1024,
    )
    result = asyncio.run(ex.execute(
        "skills.calc.ok(1)\nskills.calc.ok(2)\n"
        "try:\n    skills.calc.boom()\nexcept ValueError:\n    pass\n",
        timeout=5,
    ))
    assert result["success"] is True, result["error"]
    calls = [(e["skill"], e["fn"], e["success"]) for e in recorder.events
             if e["kind"] == "skill_call"]
    assert calls == [("calc", "ok", True), ("calc", "ok", True), ("calc", "boom", False)]


def test_next_run_does_not_repeat_previous_calls(tmp_path):
    recorder = _Recorder()
    ex = CodeExecutor(_Pool(), skills=_skills(tmp_path), recorder=recorder,
                      isolation="inprocess")
    asyncio.run(ex.execute("skills.calc.ok(1)", timeout=5))
    asyncio.run(ex.execute("x = 1", timeout=5))
    assert sum(e["kind"] == "skill_call" for e in recorder.events) == 1


def _ev(fn, ok, ts):
    return {"kind": "skill_call", "skill": "calc", "fn": fn, "success": ok, "ts": ts}


def test_report_marks_unused_and_failing():
    n = SKILL_TRIM_MIN_CALLS
    fails = int(n * SKILL_TRIM_ERROR_SHARE) + 1
    events = (
        [_ev("boom", False, f"2026-10-0{i}T00:00:00Z") for i in range(1, fails + 1)]
        + [_ev("boom", True, "2026-10-09T00:00:00Z")] * (n - fails)
        + [_ev("ok", True, "2026-10-20T00:00:00Z")] * n
        + [{"kind": "tool_call", "server": "s", "tool": "t"}]
    )
    report = summarize_skills(events, {"calc": ["boom", "idle", "ok"]})
    by_fn = {r["fn"]: r for r in report["rows"]}
    assert by_fn["idle"]["calls"] == 0 and by_fn["idle"]["candidate"] == "unused"
    assert by_fn["boom"]["candidate"] == "failing"
    assert by_fn["boom"]["error_pct"] == round(fails / n * 100, 1)
    assert by_fn["ok"] == {**by_fn["ok"], "calls": n, "error_pct": 0.0, "candidate": None,
                           "last_call": "2026-10-20T00:00:00Z"}
    assert report["candidates"] == 2
    assert report["thresholds"]["min_calls"] == SKILL_TRIM_MIN_CALLS


def test_few_calls_are_not_judged_failing():
    events = [_ev("boom", False, "2026-10-01T00:00:00Z")]
    report = summarize_skills(events, {"calc": ["boom"]})
    assert report["rows"][0]["candidate"] is None


def test_uninstalled_skill_keeps_its_row():
    report = summarize_skills([_ev("gone", True, "2026-10-01T00:00:00Z")], {})
    assert [(r["skill"], r["fn"], r["calls"]) for r in report["rows"]] == [("calc", "gone", 1)]
