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
        await asyncio.sleep(kwargs.get("delay", 0.5))
        if kwargs.get("big"):
            return _Result("x" * 1_500_000)
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


def test_big_args_and_replies_in_flight_do_not_deadlock():
    code = (
        "blob = 'y' * 1_200_000\n"
        "r = await asyncio.gather(*[srv.srv_get(blob=blob, big=True, delay=0.05)"
        " for _ in range(4)])\n"
        "print([len(x) for x in r])\n"
    )
    result, wall = _run(code, raw_cap=10_000_000)
    assert result["success"] is True, result
    assert "[1500000, 1500000, 1500000, 1500000]" in result["output"]


def test_unpicklable_reply_fails_only_its_awaiter_fast(monkeypatch):
    orig = CodeExecutor._build_proxies

    def build(self, *a, **kw):
        proxies, skill_proxies = orig(self, *a, **kw)
        for side in (proxies, skill_proxies):
            real = side["srv"].srv_get

            async def get(_real=real, **kwargs):
                if kwargs.get("bad"):
                    return lambda: 1  # not picklable
                return await _real(**kwargs)

            side["srv"].srv_get = get
        return proxies, skill_proxies

    monkeypatch.setattr(CodeExecutor, "_build_proxies", build)
    code = (
        "r = await asyncio.gather(srv.srv_get(n=1), srv.srv_get(bad=True),"
        " return_exceptions=True)\n"
        "print([type(x).__name__ for x in r])\n"
    )
    result, wall = _run(code)
    assert result["success"] is True, result
    assert "['str', 'PicklingError']" in result["output"]
    assert wall < 8, wall


def test_fire_and_forget_call_completes_and_is_counted():
    code = (
        "t = asyncio.gather(srv.srv_get(n=7))\n"
        "await asyncio.sleep(0)\n"
        "print('left')\n"
    )
    result, _ = _run(code)
    assert result["success"] is True, result
    assert "[cr] 1 tool call" in result["output"], result["output"]


def test_cancelled_awaiter_does_not_poison_next_call():
    code = (
        "try:\n"
        "    await asyncio.wait_for(srv.srv_get(n=1, delay=2), 0.2)\n"
        "except TimeoutError:\n"
        "    print('timed')\n"
        "print(await srv.srv_get(n=2, delay=0.3))\n"
    )
    result, wall = _run(code)
    assert result["success"] is True, result
    assert "timed" in result["output"] and "ok-2" in result["output"]
    assert "ok-1" not in result["output"]
    assert wall < 6, wall
