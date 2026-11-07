"""Concurrent proxy calls from the isolation child overlap end-to-end."""

import asyncio
import sys
import time

import pytest
from mcp.types import Tool

from code_runner.executor import CodeExecutor

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")


class _Content:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, text, is_error=False):
        self.isError = is_error
        self.content = [_Content(text)]


class _SlowSession:
    async def call_tool(self, name, kwargs):
        await asyncio.sleep(0.5)
        if kwargs.get("fail"):
            return _Result("boom", is_error=True)
        return _Result(f"ok-{kwargs.get('n')}")


def _executor():
    class FakePool:
        sessions = {"srv": _SlowSession()}
        tools = {
            "srv": [
                Tool(name="srv_get", inputSchema={"type": "object"}),
                Tool(name="srv_createIssue", inputSchema={"type": "object"}),
            ]
        }

    return CodeExecutor(FakePool(), isolation="subprocess", mem_limit_mb=1024)


def _run(code, **kw):
    t0 = time.monotonic()
    result = asyncio.run(_executor().execute(code, timeout=10, **kw))
    return result, time.monotonic() - t0


def test_gather_of_three_calls_overlaps():
    code = (
        "t0 = time.monotonic()\n"
        "r = await asyncio.gather(*[srv.srv_get(n=i) for i in range(3)])\n"
        "print(r, 'WALL', time.monotonic() - t0)\n"
    )
    result, _ = _run(code)
    assert result["success"] is True, result
    for i in range(3):
        assert f"ok-{i}" in result["output"]
    wall = float(result["output"].rsplit("WALL", 1)[1].strip().split()[0])
    assert wall < 1.0, wall  # measured in the child: excludes process start-up


def test_error_in_one_call_reaches_only_its_awaiter():
    code = (
        "r = await asyncio.gather(\n"
        "    srv.srv_get(n=1), srv.srv_get(fail=True), srv.srv_get(n=3),\n"
        "    return_exceptions=True)\n"
        "print([type(x).__name__ for x in r])\n"
        "print([x for x in r if isinstance(x, str)])\n"
    )
    result, _ = _run(code)
    assert result["success"] is True, result
    assert "['str', 'MCPToolError', 'str']" in result["output"]
    assert "ok-1" in result["output"] and "ok-3" in result["output"]


def test_budget_one_with_two_concurrent_calls_refuses_exactly_one():
    code = (
        "r = await asyncio.gather(srv.srv_createIssue(n=1), srv.srv_createIssue(n=2),\n"
        "    return_exceptions=True)\n"
        "print(sorted(type(x).__name__ for x in r))\n"
    )
    result, _ = _run(code, allow_side_effects=["srv.srv_createIssue:1"])
    assert result["success"] is True, result
    assert "['PermissionError', 'str']" in result["output"], result["output"]
