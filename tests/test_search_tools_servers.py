"""Ресёрч #8 (MCP-Zero, 2506.01056): двухступенчатый search_tools.

level="servers" — сервер на строку, без тулов-стабов; server="..." — поиск
внутри одного сервера.
"""

import asyncio

import pytest
from mcp.types import Tool

from code_runner.server import _search_tools_logic, mcp


def _tool(name, description=""):
    return Tool(name=name, description=description, inputSchema={"type": "object"})


TOOLS = {
    "linear": [
        _tool("linear_searchIssues", "Search issues by text"),
        _tool("linear_createIssue", "Create a new issue in a team"),
        _tool("linear_getTeams", "List teams"),
    ],
    "postgres-lime": [
        _tool("execute_sql", "Run a SQL query against the database"),
        _tool("list_schemas", "List database schemas"),
    ],
    "filesystem": [_tool("read_text_file", "Read a text file from disk")],
}
PY = {"linear": "linear", "postgres_lime": "postgres-lime", "filesystem": "filesystem"}


def test_servers_level_one_line_per_server_best_first():
    out = _search_tools_logic("sql database", TOOLS, PY, level="servers")
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert lines[0].startswith("- postgres_lime (2 tools, 2 match)")
    assert "execute_sql" in lines[0]
    assert all("linear" not in ln for ln in lines)  # нет совпадения — нет строки
    assert "await" not in out and "Args:" not in out
    assert 'server="postgres_lime"' in out  # следующий шаг назван


def test_servers_level_matches_server_name():
    out = _search_tools_logic("linear", TOOLS, PY, level="servers")
    assert out.splitlines()[0].startswith("- linear (3 tools")


def test_server_filter_restricts_search():
    out = _search_tools_logic("list", TOOLS, PY, detail="name", server="postgres-lime")
    assert "postgres_lime.list_schemas" in out
    assert "linear_getTeams" not in out
    # py-имя работает так же, как исходное
    assert out == _search_tools_logic("list", TOOLS, PY, detail="name", server="postgres_lime")


def test_server_filter_empty_query_lists_whole_server():
    out = _search_tools_logic("", TOOLS, PY, detail="full", server="linear")
    assert out.count("linear.linear_") == 3
    assert "await" not in out  # без запроса стабы не отдаются


def test_unknown_server_names_available():
    out = _search_tools_logic("x", TOOLS, PY, server="nope")
    assert "Unknown server 'nope'" in out and "postgres_lime" in out


def test_unknown_level_rejected():
    with pytest.raises(ValueError, match="level must be one of"):
        _search_tools_logic("sql", TOOLS, PY, level="skills")


def test_tool_schema_exposes_level_and_server():
    tools = asyncio.run(mcp.list_tools())
    props = next(t for t in tools if t.name == "search_tools").inputSchema["properties"]
    assert props["level"]["enum"] == ["tools", "servers"]
    assert "server" in props
