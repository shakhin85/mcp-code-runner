"""SHA-523: a failed skill names the raw tools from its `replaces:` as fallback.

macro-to-micro recovery (arXiv 2610.31461): the skill wrapper fails → the
caller gets the primitive tool-call path instead of a dead end, and
`get_metrics(skills=True)` reports how often the next block took it.
"""

import asyncio
import sys

import pytest

from code_runner.executor import CodeExecutor
from code_runner.metrics import MetricsRecorder, summarize_fallbacks, summarize_skills
from code_runner.skills import SkillLoader, SkillsNamespace, mark_fatal

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
        "async def fetch():\n"
        "    await _cr_servers['srv'].read_items()\n"
        "    raise RuntimeError('schema drift')\n"
        "def store(tag):\n"
        "    raise ValueError('tag обязателен')\n",
        encoding="utf-8",
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
    result = asyncio.run(ex.execute("await skills.wrap.fetch()", timeout=5))
    assert result["success"] is False
    assert "RuntimeError: schema drift" in result["error"]
    assert "HINT: skills.wrap.fetch упал; fallback — raw-tools из replaces: srv.read_items" \
        in result["error"]


def test_skill_without_replaces_gets_no_fallback_hint(tmp_path):
    ex = _executor(tmp_path, "name: wrap\n")
    result = asyncio.run(ex.execute("await skills.wrap.fetch()", timeout=5))
    assert result["success"] is False
    assert "fallback" not in result["error"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_skill_failed_before_any_tool_call_gets_no_fallback(tmp_path, isolation):
    # Bad input: the raw tool would take the same input and skip the skill's check.
    recorder = _Recorder()
    ex = _executor(
        tmp_path, "replaces: [srv.read_items:store]\n", recorder=recorder, isolation=isolation
    )
    result = asyncio.run(ex.execute("skills.wrap.store(None)", timeout=5, session_id="s1"))
    assert "ValueError: tag обязателен" in result["error"]
    assert "fallback" not in result["error"]
    call = next(e for e in recorder.events if e["kind"] == "skill_call")
    assert call["tool_calls"] == 0
    assert "fallback" not in call


@pytest.mark.parametrize("isolation", ISOLATIONS)
@pytest.mark.parametrize("raised", ["ValueError('x')", "RuntimeError('other')"])
def test_caught_skill_error_adds_no_hint(tmp_path, isolation, raised):
    # Same class as the skill's error included: only the skill's own exception counts.
    ex = _executor(tmp_path, "replaces: [srv.read_items:fetch]\n", isolation=isolation)
    result = asyncio.run(ex.execute(
        f"try:\n    await skills.wrap.fetch()\nexcept RuntimeError:\n    pass\nraise {raised}",
        timeout=5,
    ))
    assert result["success"] is False
    assert "fallback" not in result["error"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_reraised_skill_error_keeps_hint(tmp_path, isolation):
    ex = _executor(tmp_path, "replaces: [srv.read_items:fetch]\n", isolation=isolation)
    result = asyncio.run(ex.execute(
        "try:\n    await skills.wrap.fetch()\nexcept RuntimeError:\n    raise", timeout=5,
    ))
    assert "fallback — raw-tools из replaces: srv.read_items" in result["error"]


def test_next_block_raw_call_counts_as_recovered(tmp_path):
    recorder = _Recorder()
    ex = _executor(tmp_path, "replaces: [srv.read_items:fetch]\n", recorder=recorder)
    asyncio.run(ex.execute("await skills.wrap.fetch()", timeout=5, session_id="s1"))
    ok = asyncio.run(ex.execute("await srv.read_items()", timeout=5, session_id="s1"))
    assert ok["success"] is True, ok["error"]

    failed = [e for e in recorder.events if e["kind"] == "skill_call"]
    assert failed[0]["fallback"] == ["srv.read_items"]
    assert recorder.events[-1]["ok_tools"] == ["srv.read_items"]
    report = summarize_skills(recorder.events, {"wrap": ["fetch"]})
    assert report["fallback"] == {"failures": 1, "recovered": 1, "share": 1.0}


def test_fallback_share_counts_unrecovered_and_ignores_other_sessions():
    fail = {"kind": "skill_call", "skill": "w", "fn": "f", "success": False,
            "fatal": True, "fallback": ["srv.t"]}
    events = [
        {**fail, "session_id": "a"},
        {"kind": "execute_code", "session_id": "b", "ok_tools": ["srv.t"]},
        {"kind": "execute_code", "session_id": "a", "ok_tools": ["srv.other"]},
        {**fail, "session_id": "b"},
        {"kind": "execute_code", "session_id": "b", "ok_tools": ["srv.t"]},
        {**fail, "session_id": None},
    ]
    assert summarize_fallbacks(events) == {"failures": 2, "recovered": 1, "share": 0.5}


def _inner_call_skills(tmp_path):
    d = tmp_path / "skills" / "wrap"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nreplaces: [srv.read_items:fetch]\n---\nwrap\n", encoding="utf-8"
    )
    (d / "script.py").write_text(
        "async def fetch():\n"
        "    await _cr_servers['srv'].read_items()\n"
        "    raise RuntimeError('after inner call')\n",
        encoding="utf-8",
    )
    return SkillsNamespace(SkillLoader(tmp_path / "skills").discover())


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_skill_inner_raw_call_is_not_ok_tool(tmp_path, isolation):
    # A skill's own raw call must not make its retry in the next block "recovered".
    recorder = _Recorder()
    pool = _FakePool(
        sessions={"srv": _FakeSession('{"rows": []}')},
        tools={"srv": [_FakeTool("read_items")]},
    )
    ex = CodeExecutor(
        pool, skills=_inner_call_skills(tmp_path), recorder=recorder,
        isolation=isolation, mem_limit_mb=1024,
    )
    for _ in range(2):
        asyncio.run(ex.execute("await skills.wrap.fetch()", timeout=5, session_id="s1"))
    blocks = [e for e in recorder.events if e["kind"] == "execute_code"]
    assert [b["ok_tools"] for b in blocks] == [[], []]
    report = summarize_skills(recorder.events, {"wrap": ["fetch"]})
    assert report["fallback"]["recovered"] == 0


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_caught_skill_error_has_no_fallback_field(tmp_path, isolation):
    recorder = _Recorder()
    ex = _executor(
        tmp_path, "replaces: [srv.read_items:fetch]\n", recorder=recorder, isolation=isolation
    )
    asyncio.run(ex.execute(
        "try:\n    await skills.wrap.fetch()\nexcept RuntimeError:\n    pass",
        timeout=5, session_id="s1",
    ))
    call = next(e for e in recorder.events if e["kind"] == "skill_call")
    assert call["success"] is False
    assert "fallback" not in call
    assert summarize_fallbacks(recorder.events)["failures"] == 0


def test_fallback_share_ignores_non_fatal_failed_call():
    events = [
        {"kind": "skill_call", "session_id": "a", "success": False, "fallback": ["srv.t"]},
        {"kind": "execute_code", "session_id": "a", "ok_tools": []},
    ]
    assert summarize_fallbacks(events)["failures"] == 0


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_raise_from_skill_error_keeps_hint(tmp_path, isolation):
    ex = _executor(tmp_path, "replaces: [srv.read_items:fetch]\n", isolation=isolation)
    result = asyncio.run(ex.execute(
        "try:\n    await skills.wrap.fetch()\nexcept RuntimeError as e:\n"
        "    raise ValueError('wrapped') from e",
        timeout=5,
    ))
    assert "fallback — raw-tools из replaces: srv.read_items" in result["error"]


def test_mark_fatal_follows_cause_chain_and_survives_cycle():
    ev_a, ev_b = {"skill": "a"}, {"skill": "b"}
    inner = RuntimeError("inner")
    inner.__dict__["_cr_skill_events"] = [ev_a]
    outer = ValueError("outer")
    outer.__cause__ = inner
    inner.__cause__ = outer
    outer.__dict__["_cr_skill_events"] = [ev_b]
    mark_fatal(outer)
    assert ev_a["fatal"] and ev_b["fatal"]


def test_mark_fatal_ignores_implicit_context():
    ev = {"skill": "a"}
    inner = RuntimeError("inner")
    inner.__dict__["_cr_skill_events"] = [ev]
    outer = ValueError("outer")
    outer.__context__ = inner
    mark_fatal(outer)
    assert "fatal" not in ev


def test_skills_report_reads_only_skill_and_block_events(tmp_path):
    rec = MetricsRecorder(tmp_path / "m.jsonl")
    for kind in ("tool_call", "skill_call", "execute_code", "tool_call"):
        rec.record({"kind": kind, "ts": "2999-01-01T00:00:00"})
    got = rec.read(kind=("skill_call", "execute_code"), limit=0)
    assert [e["kind"] for e in got] == ["skill_call", "execute_code"]
