"""Observed result shape: keys and types only, never values; shown by the stub."""

import asyncio

import pytest
from mcp.types import Tool

from code_runner.executor import CodeExecutor
from code_runner.observed_shape import SHAPE_MAX, describe, lookup, record, shapes_path
from code_runner.schema_gen import tool_to_stub

SECRET = "sk-SECRET-VALUE-123"


class _Content:
    def __init__(self, text):
        self.text = text


class _Result:
    isError = False
    structuredContent = None

    def __init__(self, text):
        self.content = [_Content(text)]


class _Session:
    async def call_tool(self, name, kwargs):
        if name == "json_text":
            return _Result(f'{{"rows": [{{"id": 1, "token": "{SECRET}"}}], "n": 1}}')
        return _Result(f"plain prose with {SECRET}")


class _Pool:
    sessions = {"srv": _Session()}
    tools = {
        "srv": [
            Tool(name="json_text", inputSchema={"type": "object"}),
            Tool(name="prose", inputSchema={"type": "object"}),
        ]
    }


def test_describe_keys_and_types_depth_two():
    value = {"rows": [{"id": 1, "name": "x"}], "meta": {"a": {"b": 1}}, "ok": True}
    assert describe(value) == "{rows: list[{id: int, name: str}], meta: {a: dict}, ok: bool}"


def test_describe_list_uses_first_element_and_caps_length():
    assert describe([{"a": 1}, {"b": 2}]) == "list[{a: int}]"
    assert len(describe({f"key{i}": i for i in range(200)})) == SHAPE_MAX


def test_describe_prose():
    assert describe("hello") == "str (не JSON)"


def test_record_stores_no_values():
    record("srv", "t", {"password": SECRET, "items": [SECRET]})
    assert SECRET not in shapes_path().read_text(encoding="utf-8")
    assert lookup("srv", "t") == "{password: str, items: list[str]}"


def test_stub_shows_observed_shape_and_unknown_stays_unchanged():
    tool = Tool(name="t", inputSchema={"type": "object"})
    assert "Returns" not in tool_to_stub("srv", tool)
    record("srv", "t", {"a": 1})
    assert "# Returns (observed): {a: int}" in tool_to_stub("srv", tool)


def test_output_schema_wins_over_observed():
    record("srv", "t", {"a": 1})
    tool = Tool(
        name="t",
        inputSchema={"type": "object"},
        outputSchema={"type": "object", "properties": {"count": {"type": "integer"}}},
    )
    stub = tool_to_stub("srv", tool)
    assert "# Returns: {count: int}" in stub and "observed" not in stub


@pytest.mark.parametrize("isolation", ["inprocess", "subprocess"])
def test_call_records_shape_from_json_text_and_prose(isolation):
    ex = CodeExecutor(_Pool(), isolation=isolation, mem_limit_mb=1024)
    r = asyncio.run(ex.execute("await srv.json_text()\nawait srv.prose()", timeout=5))
    assert r["success"] is True, r["error"]
    assert lookup("srv", "json_text") == "{rows: list[{id: int, token: str}], n: int}"
    assert lookup("srv", "prose") == "str (не JSON)"
    assert SECRET not in shapes_path().read_text(encoding="utf-8")
