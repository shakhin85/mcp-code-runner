"""SHA-129: a downstream MCP server that never answers must not hang execute_code.

Symptom in the field: `skills.forgetful_create.memory(...)` inside execute_code hung
for minutes while a direct HTTP call to forgetful took 0.9 s. Each downstream
tool-call now carries its own per_call_timeout, and the error names server + tool.
The overall execute_code timeout stays the upper bound.
"""

import asyncio
import sys
import time

import pytest
from mcp.types import Tool

from code_runner.executor import CodeExecutor


class _HangingSession:
    """Downstream that accepts the call and never responds."""

    async def call_tool(self, name, kwargs):
        await asyncio.Event().wait()


def _pool():
    class FakePool:
        sessions = {"forgetful": _HangingSession()}
        tools = {"forgetful": [Tool(name="create_memory", inputSchema={"type": "object"})]}
    return FakePool()


ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_per_call_timeout_names_server_and_tool(isolation):
    ex = CodeExecutor(_pool(), isolation=isolation, mem_limit_mb=1024)
    start = time.monotonic()
    result = asyncio.run(ex.execute(
        "await forgetful.create_memory(title='x')",
        timeout=20,
        per_call_timeout=0.5,
    ))
    elapsed = time.monotonic() - start
    assert result["success"] is False
    assert "TimeoutError" in result["error"], result["error"]
    assert "forgetful.create_memory > 0.5s" in result["error"], result["error"]
    assert elapsed <= 0.5 + 2, f"hung call not bounded, took {elapsed:.2f}s"


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_per_call_timeout_is_catchable_and_run_continues(isolation):
    ex = CodeExecutor(_pool(), isolation=isolation, mem_limit_mb=1024)
    code = (
        "try:\n"
        "    await forgetful.create_memory(title='x')\n"
        "except Exception as e:\n"
        "    print('caught', type(e).__name__)\n"
        "print('after')"
    )
    result = asyncio.run(ex.execute(code, timeout=20, per_call_timeout=0.3))
    assert result["success"] is True, result["error"]
    assert "caught TimeoutError" in result["output"]
    assert "after" in result["output"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_execute_timeout_stays_upper_bound_over_per_call_timeout(isolation):
    ex = CodeExecutor(_pool(), isolation=isolation, mem_limit_mb=1024)
    start = time.monotonic()
    result = asyncio.run(ex.execute(
        "await forgetful.create_memory(title='x')",
        timeout=1,
        per_call_timeout=30,
    ))
    elapsed = time.monotonic() - start
    assert result["success"] is False
    assert "timed out" in result["error"].lower(), result["error"]
    assert elapsed < 1 + 2, f"execute timeout did not bound the call, took {elapsed:.2f}s"


def test_per_call_timeout_defaults_to_30s():
    import inspect
    sig = inspect.signature(CodeExecutor.execute)
    assert sig.parameters["per_call_timeout"].default == 30.0
