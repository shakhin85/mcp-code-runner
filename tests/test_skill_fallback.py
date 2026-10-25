"""SHA-523: a failed skill names the raw tools from its `replaces:` as fallback.

macro-to-micro recovery (arXiv 2610.31461): the skill wrapper fails → the
caller gets the primitive tool-call path instead of a dead end, and
`get_metrics(skills=True)` reports how often the next block took it.
"""

import asyncio
import sys

import pytest

from code_runner.executor import CodeExecutor
from code_runner.metrics import summarize_fallbacks, summarize_skills
from code_runner.skills import SkillLoader, SkillsNamespace

from .test_metrics_integration import _FakePool, _FakeSession, _FakeTool

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


def _skills(tmp_path, frontmatter):
    d = tmp_path / "skills" / "wrap"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}---\nwrap\n", encoding="utf-8")
    (d / "script.py").write_text(
        "def fetch():\n    raise RuntimeError('schema drift')\n", encoding="utf-8"
    )
    return SkillsNamespace(SkillLoader(tmp_path / "skills").discover())


def _executor(tmp_path, frontmatter, recorder=None, isolation="inprocess"):
    pool = _FakePool(
        sessions={"srv": _FakeSession('{"rows": []}')},
        tools={"srv": [_FakeTool("read_items")]},
    )
    return CodeExecutor(
        pool, skills=_skills(tmp_path, frontmatter), recorder=recorder,
        isolation=isolation, mem_limit_mb=1024,
    )


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_failed_skill_names_replaced_raw_tool(tmp_path, isolation):
    ex = _executor(tmp_path, "replaces: [srv.read_items:fetch]\n", isolation=isolation)
    result = asyncio.run(ex.execute("skills.wrap.fetch()", timeout=5))
    assert result["success"] is False
    assert "RuntimeError: schema drift" in result["error"]
    assert "HINT: skills.wrap.fetch упал; fallback — raw-tools из replaces: srv.read_items" \
        in result["error"]


def test_skill_without_replaces_gets_no_fallback_hint(tmp_path):
    ex = _executor(tmp_path, "name: wrap\n")
    result = asyncio.run(ex.execute("skills.wrap.fetch()", timeout=5))
    assert result["success"] is False
    assert "fallback" not in result["error"]


def test_caught_skill_error_adds_no_hint(tmp_path):
    ex = _executor(tmp_path, "replaces: [srv.read_items:fetch]\n")
    result = asyncio.run(ex.execute(
        "try:\n    skills.wrap.fetch()\nexcept RuntimeError:\n    pass\nraise ValueError('x')",
        timeout=5,
    ))
    assert result["success"] is False
    assert "fallback" not in result["error"]


def test_next_block_raw_call_counts_as_recovered(tmp_path):
    recorder = _Recorder()
    ex = _executor(tmp_path, "replaces: [srv.read_items:fetch]\n", recorder=recorder)
    asyncio.run(ex.execute("skills.wrap.fetch()", timeout=5, session_id="s1"))
    ok = asyncio.run(ex.execute("await srv.read_items()", timeout=5, session_id="s1"))
    assert ok["success"] is True, ok["error"]

    failed = [e for e in recorder.events if e["kind"] == "skill_call"]
    assert failed[0]["fallback"] == ["srv.read_items"]
    assert recorder.events[-1]["ok_tools"] == ["srv.read_items"]
    report = summarize_skills(recorder.events, {"wrap": ["fetch"]})
    assert report["fallback"] == {"failures": 1, "recovered": 1, "share": 1.0}


def test_fallback_share_counts_unrecovered_and_ignores_other_sessions():
    fail = {"kind": "skill_call", "skill": "w", "fn": "f", "success": False,
            "fallback": ["srv.t"]}
    events = [
        {**fail, "session_id": "a"},
        {"kind": "execute_code", "session_id": "b", "ok_tools": ["srv.t"]},
        {"kind": "execute_code", "session_id": "a", "ok_tools": ["srv.other"]},
        {**fail, "session_id": "b"},
        {"kind": "execute_code", "session_id": "b", "ok_tools": ["srv.t"]},
        {**fail, "session_id": None},
    ]
    assert summarize_fallbacks(events) == {"failures": 2, "recovered": 1, "share": 0.5}
