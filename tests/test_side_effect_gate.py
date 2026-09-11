"""SHA-123: side-effecting MCP tools reached from inside execute_code must go
through the same approval gate as direct MCP calls. Today the PreToolUse hooks
guard only direct calls; `await bitrix.crm_deal_add(...)` from sandbox code
bypassed them. The proxy choke point (`_ToolNamespace._make_wrapper`) now
denies a tool whose name looks side-effecting (add/create/update/delete/remove)
unless the caller opts in via `allow_side_effects`. Harness mirrors
tests/test_per_call_timeout.py.
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
    """Downstream that records every call it actually receives."""

    def __init__(self):
        self.calls = []

    async def call_tool(self, name, kwargs):
        self.calls.append((name, kwargs))
        return _Result()


def _pool():
    session = _CountingSession()

    class FakePool:
        sessions = {"forgetful": session}
        tools = {
            "forgetful": [
                Tool(name="create_memory", inputSchema={"type": "object"}),
                Tool(name="query_memory", inputSchema={"type": "object"}),
            ]
        }

    return FakePool(), session


ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_side_effecting_tool_denied_by_default(isolation):
    pool, session = _pool()
    ex = CodeExecutor(pool, isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(
        ex.execute("await forgetful.create_memory(title='x')", timeout=5)
    )
    assert result["success"] is False
    assert "PermissionError" in result["error"], result["error"]
    assert (
        'forgetful.create_memory is side-effecting; pass '
        'allow_side_effects=["forgetful.create_memory"] to execute_code'
    ) in result["error"], result["error"]
    assert session.calls == [], "a refused call must never reach the downstream"


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_side_effecting_tool_allowed_explicitly(isolation):
    pool, session = _pool()
    ex = CodeExecutor(pool, isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(ex.execute(
        "await forgetful.create_memory(title='x')",
        timeout=5,
        allow_side_effects=["forgetful.create_memory"],
    ))
    assert result["success"] is True, result["error"]
    assert session.calls == [("create_memory", {"title": "x"})]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_side_effecting_tool_allowed_via_wildcard(isolation):
    pool, session = _pool()
    ex = CodeExecutor(pool, isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(ex.execute(
        "await forgetful.create_memory(title='x')",
        timeout=5,
        allow_side_effects=["forgetful.*"],
    ))
    assert result["success"] is True, result["error"]
    assert len(session.calls) == 1


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_read_tool_is_never_gated(isolation):
    pool, session = _pool()
    ex = CodeExecutor(pool, isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(
        ex.execute("await forgetful.query_memory(q='x')", timeout=5)
    )
    assert result["success"] is True, result["error"]
    assert session.calls == [("query_memory", {"q": "x"})]


def test_regex_matches_name_tokens_not_substrings():
    """Documents the actual behavior of _SIDE_EFFECT_RE, including its gap.

    Matched: a verb bounded by start/end or a `._-` separator.
    NOT matched: `execute_sql`/`query_memory` (no verb token at all), or a
    verb glued into camelCase with no separator (`linear_createIssue`'s
    verb-looking substring is "createIssue", not "create" — known gap,
    called out in the comment on _SIDE_EFFECT_RE for a later ticket).
    """
    assert _is_side_effecting("crm.deal.add") is True
    assert _is_side_effecting("linear.delete_issue") is True
    assert _is_side_effecting("create_memory") is True
    assert _is_side_effecting("linear_createIssue") is False
    assert _is_side_effecting("query_memory") is False
    assert _is_side_effecting("execute_sql") is False
