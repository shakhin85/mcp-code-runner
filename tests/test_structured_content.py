"""MCP 2025-06-18: structuredContent is the call's value; outputSchema shows in the stub."""

import asyncio
import sys

import pytest
from mcp.types import Tool

from code_runner.executor import CodeExecutor
from code_runner.schema_gen import tool_to_stub
from code_runner.server import _search_tools_logic


class _Content:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, text, structured=None):
        self.content = [_Content(text)]
        self.isError = False
        self.structuredContent = structured


class _Session:
    async def call_tool(self, name, kwargs):
        if name == "typed":
            return _Result("TEXT-COPY-IGNORED", {"rows": [{"a": 1}], "count": 1})
        if name == "wrapped":
            return _Result("42", {"result": 42})
        if name == "wrapped_no_flag":
            return _Result("7", {"result": 7})
        if name == "empty":
            return _Result("[]", {"result": []})
        return _Result('{"old": true}')


class _Pool:
    sessions = {"srv": _Session()}
    tools = {
        "srv": [
            Tool(name="typed", inputSchema={"type": "object"}),
            Tool(
                name="wrapped",
                inputSchema={"type": "object"},
                outputSchema={
                    "type": "object",
                    "properties": {"result": {"type": "integer"}},
                    "x-fastmcp-wrap-result": True,
                },
            ),
            Tool(
                name="empty",
                inputSchema={"type": "object"},
                outputSchema={
                    "type": "object",
                    "properties": {"result": {"type": "array"}},
                    "x-fastmcp-wrap-result": True,
                },
            ),
            Tool(name="wrapped_no_flag", inputSchema={"type": "object"}),
            Tool(name="plain", inputSchema={"type": "object"}),
        ]
    }


ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]


def _run(isolation, code):
    ex = CodeExecutor(_Pool(), isolation=isolation, mem_limit_mb=1024)
    return asyncio.run(ex.execute(code, timeout=5))


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_structured_content_is_the_value(isolation):
    r = _run(isolation, "d = await srv.typed()\nprint(type(d).__name__, d['count'], d['rows'])")
    assert r["success"] is True, r["error"]
    assert "dict 1 [{'a': 1}]" in r["output"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_wrap_result_flag_unwraps(isolation):
    r = _run(isolation, "v = await srv.wrapped()\nprint(repr(v))")
    assert r["success"] is True, r["error"]
    assert "42" in r["output"] and "result" not in r["output"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_result_key_without_flag_stays_dict(isolation):
    r = _run(isolation, "v = await srv.wrapped_no_flag()\nprint(repr(v))")
    assert "{'result': 7}" in r["output"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_text_path_unchanged_without_structured_content(isolation):
    r = _run(isolation, "v = await srv.plain()\nprint(repr(v))")
    assert "{'old': True}" in r["output"]


def test_empty_structured_list_counts_as_quiet_failure():
    ex = CodeExecutor(_Pool(), isolation="inprocess")
    r = asyncio.run(ex.execute("v = await srv.empty()\nprint(repr(v))", timeout=5))
    assert "[]" in r["output"]
    assert "0 rows" in r["output"] or "empty" in r["output"].lower()


def test_stub_shows_object_return_type():
    tool = Tool(
        name="q",
        inputSchema={"type": "object"},
        outputSchema={
            "type": "object",
            "properties": {"rows": {"type": "array"}, "count": {"type": "integer"}},
        },
    )
    assert "# Returns: {rows: list, count: int}" in tool_to_stub("srv", tool)


def test_stub_shows_inner_type_for_wrapped_result():
    tool = Tool(
        name="q",
        inputSchema={"type": "object"},
        outputSchema={
            "type": "object",
            "properties": {"result": {"type": "integer"}},
            "x-fastmcp-wrap-result": True,
        },
    )
    assert "# Returns: int" in tool_to_stub("srv", tool)


def test_stub_without_output_schema_has_no_returns():
    tool = Tool(name="q", inputSchema={"type": "object"})
    assert "Returns" not in tool_to_stub("srv", tool)


def test_search_tools_renders_returns():
    tool = Tool(
        name="count_rows",
        description="count rows",
        inputSchema={"type": "object"},
        outputSchema={"type": "object", "properties": {"count": {"type": "integer"}}},
    )
    out = _search_tools_logic("count_rows", {"srv": [tool]}, {"srv": "srv"})
    assert "# Returns: {count: int}" in out


class _TaintSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, kwargs):
        if name == "fetch":
            # every line < TAINT_MIN_MATCH: each 20-char window crosses a "\n",
            # which the JSON dump escapes — only raw-string taint catches it.
            page = "\n".join(f"short line {i}" for i in range(6))
            return _Result("TEXT-COPY-IGNORED", {"page": page})
        self.calls.append((name, kwargs))
        return _Result("ok")


class _TaintPool:
    def __init__(self):
        self.sink = _TaintSession()
        self.sessions = {"wigolo": _TaintSession(), "forgetful": self.sink}
        self.tools = {
            "wigolo": [Tool(name="fetch", inputSchema={"type": "object"})],
            "forgetful": [Tool(name="create_memory", inputSchema={"type": "object"})],
        }


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_structured_untrusted_string_is_tainted_as_returned(isolation):
    pool = _TaintPool()
    ex = CodeExecutor(pool, isolation=isolation, mem_limit_mb=1024)
    r = asyncio.run(ex.execute(
        "d = await wigolo.fetch(url='u')\n"
        "await forgetful.create_memory(title='t', content=d['page'])",
        timeout=10,
        allow_side_effects=["forgetful.create_memory"],
    ))
    assert r["success"] is False
    assert "PermissionError" in r["error"] and "wigolo" in r["error"], r["error"]
    assert pool.sink.calls == []


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_raw_capped_structured_json_is_tainted(isolation):
    # Over raw_cap the call returns the JSON dump as a string: its escaped "\n"
    # never matches the raw leaves, so the dump itself must be recorded.
    pool = _TaintPool()
    ex = CodeExecutor(pool, isolation=isolation, mem_limit_mb=1024)
    r = asyncio.run(ex.execute(
        "s = await wigolo.fetch(url='u')\n"
        "await forgetful.create_memory(title='t', content=s)",
        timeout=10,
        raw_cap=60,
        allow_side_effects=["forgetful.create_memory"],
    ))
    assert r["success"] is False
    assert "PermissionError" in r["error"] and "wigolo" in r["error"], r["error"]
    assert pool.sink.calls == []
