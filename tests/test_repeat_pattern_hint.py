"""Третий успешный блок той же формы в сессии предлагает save_skill с черновиком (ресёрч #5)."""

import asyncio
import sys

import pytest

from code_runner.executor import CodeExecutor

ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]

MARK = "HINT: паттерн повторился"


class _Text:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, text):
        self.content = [_Text(text)]
        self.isError = False


class _Session:
    async def call_tool(self, name, kwargs):
        return _Result("ok")


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = ""


class _Pool:
    sessions = {"postgres": _Session()}
    tools = {"postgres": [_Tool("execute_sql")]}


def _block(table: str) -> str:
    return f"r = await postgres.execute_sql(sql='select 1 from {table}')\nprint(len(r))"


def _run(ex, code, sid="s1"):
    return asyncio.run(ex.execute(code, session_id=sid))


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_third_repeat_offers_draft_with_varying_literal_as_param(isolation):
    ex = CodeExecutor(_Pool(), isolation=isolation)
    outs = [_run(ex, _block(t))["output"] for t in ("a", "b", "c")]
    assert MARK not in outs[0] and MARK not in outs[1]
    assert MARK in outs[2]
    assert "save_skill" in outs[2]
    assert "async def run(p0)" in outs[2]
    # Литерал, который менялся между повторами, стал параметром, а не зашит в черновик.
    assert "select 1 from c" not in outs[2].split(MARK, 1)[1]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_hint_once_per_pattern(isolation):
    ex = CodeExecutor(_Pool(), isolation=isolation)
    outs = [_run(ex, _block(t))["output"] for t in "abcd"]
    assert sum(MARK in o for o in outs) == 1


def test_different_shape_does_not_count():
    ex = CodeExecutor(_Pool(), isolation="inprocess")
    _run(ex, _block("a"))
    _run(ex, _block("b"))
    out = _run(ex, "r = await postgres.execute_sql(sql='x')\nprint(r)\nprint(1)")["output"]
    assert MARK not in out


def test_block_without_tool_calls_does_not_count():
    ex = CodeExecutor(_Pool(), isolation="inprocess")
    outs = [_run(ex, f"x = {n}\nprint(x + 1)")["output"] for n in (1, 2, 3)]
    assert all(MARK not in o for o in outs)


def test_failed_block_does_not_count():
    ex = CodeExecutor(_Pool(), isolation="inprocess")
    bad = "r = await postgres.execute_sql(sql='a')\nraise ValueError('x')"
    _run(ex, bad)
    _run(ex, bad.replace("'a'", "'b'"))
    out = _run(ex, bad.replace("'a'", "'c'"))
    assert MARK not in (out["output"] + (out["error"] or ""))


def test_sessions_counted_separately():
    ex = CodeExecutor(_Pool(), isolation="inprocess")
    _run(ex, _block("a"), sid="s1")
    _run(ex, _block("b"), sid="s2")
    assert MARK not in _run(ex, _block("c"), sid="s3")["output"]
