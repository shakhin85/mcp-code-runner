"""SHA-125: gaps in the SHA-123 approval gate.

1. camelCase names with no separator before the verb (`linear_createIssue`,
   `deleteIssue`) slipped past `_SIDE_EFFECT_RE`.
2. Generic dispatchers carry the operation in an argument
   (`forgetful.execute_forgetful_tool(tool_name="create_memory")`), so the
   tool's own name says nothing about the side effect.
"""

import asyncio
import sys

import pytest
from mcp.types import Tool

from code_runner.executor import CodeExecutor, _is_side_effecting


class _Content:
    def __init__(self, text):
        self.text = text


class _Result:
    isError = False

    def __init__(self):
        self.content = [_Content("ok")]


class _CountingSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, kwargs):
        self.calls.append((name, kwargs))
        return _Result()


def _pool():
    forgetful, linear = _CountingSession(), _CountingSession()

    class FakePool:
        sessions = {"forgetful": forgetful, "linear": linear}
        tools = {
            "forgetful": [
                Tool(name="execute_forgetful_tool", inputSchema={"type": "object"}),
            ],
            "linear": [
                Tool(name="linear_createIssue", inputSchema={"type": "object"}),
                Tool(name="linear_getIssues", inputSchema={"type": "object"}),
            ],
        }

    return FakePool(), forgetful, linear


ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]


@pytest.mark.parametrize(
    "name", ["linear_createIssue", "deleteIssue", "crmDealUpdate", "removeLabel"]
)
def test_camel_case_verbs_are_side_effecting(name):
    assert _is_side_effecting(name) is True


@pytest.mark.parametrize(
    "name", ["linear_getIssues", "getAddress", "executeSql", "queryMemory"]
)
def test_camel_case_reads_are_not_gated(name):
    assert _is_side_effecting(name) is False


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_camel_case_tool_denied_by_default(isolation):
    pool, _, linear = _pool()
    ex = CodeExecutor(pool, isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(
        ex.execute("await linear.linear_createIssue(title='x')", timeout=5)
    )
    assert result["success"] is False
    assert 'allow_side_effects=["linear.linear_createIssue"]' in result["error"]
    assert linear.calls == []


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_dispatcher_classified_by_dispatched_operation(isolation):
    pool, forgetful, _ = _pool()
    ex = CodeExecutor(pool, isolation=isolation, mem_limit_mb=1024)
    code = (
        "await forgetful.execute_forgetful_tool("
        "tool_name='create_memory', arguments={'title': 'x'})"
    )
    result = asyncio.run(ex.execute(code, timeout=5))
    assert result["success"] is False
    assert (
        'forgetful.create_memory is side-effecting; pass '
        'allow_side_effects=["forgetful.create_memory"] to execute_code'
    ) in result["error"], result["error"]
    assert forgetful.calls == []

    allowed = asyncio.run(ex.execute(
        code, timeout=5, allow_side_effects=["forgetful.create_memory"]
    ))
    assert allowed["success"] is True, allowed["error"]
    assert len(forgetful.calls) == 1


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_dispatcher_read_operation_passes(isolation):
    pool, forgetful, _ = _pool()
    ex = CodeExecutor(pool, isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(ex.execute(
        "await forgetful.execute_forgetful_tool("
        "tool_name='query_memory', arguments={'query': 'x'})",
        timeout=5,
    ))
    assert result["success"] is True, result["error"]
    assert len(forgetful.calls) == 1
