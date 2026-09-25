"""Exceptions the tool proxy raises must be nameable in sandbox `except`.

The SHA-123 gate raises PermissionError and the SHA-129 per-call deadline
raises TimeoutError, but neither name was in SAFE_BUILTINS: `except
PermissionError:` itself failed with NameError.
"""

import asyncio
import sys

import pytest
from mcp.types import Tool

from code_runner.executor import CodeExecutor


class _Content:
    text = "ok"


class _Result:
    isError = False
    content = [_Content()]


class _Session:
    async def call_tool(self, name, kwargs):
        if name == "slow":
            await asyncio.sleep(5)
        return _Result()


class _Pool:
    sessions = {"srv": _Session()}
    tools = {"srv": [Tool(name="delete_row", inputSchema={"type": "object"}),
                     Tool(name="slow", inputSchema={"type": "object"})]}


ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_gate_refusal_catchable_by_name(isolation):
    ex = CodeExecutor(_Pool(), isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(ex.execute(
        "try:\n"
        "    await srv.delete_row(id=1)\n"
        "except PermissionError:\n"
        "    print('refused')\n",
        timeout=5,
    ))
    assert result["success"] is True, result["error"]
    assert result["output"].splitlines()[0] == "refused"


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_per_call_timeout_catchable_by_name(isolation):
    ex = CodeExecutor(_Pool(), isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(ex.execute(
        "try:\n"
        "    await srv.slow()\n"
        "except TimeoutError:\n"
        "    print('timed out')\n",
        timeout=5,
        per_call_timeout=0.2,
    ))
    assert result["success"] is True, result["error"]
    assert result["output"].splitlines()[0] == "timed out"
