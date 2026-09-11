"""[relay-cr-2026-09] gap-фича: search_tools(detail="name"|"desc"|"full").

Источник: anthropic.com/engineering/code-execution-with-mcp — уровни детализации
поиска, чтобы широкий запрос не отдавал полные стабы всех совпавших тулов.
"""

import pytest
from mcp.types import Tool

from code_runner.server import _search_tools_logic

TOOLS = {
    "postgres-lime": [
        Tool(
            name="execute_sql",
            description="Run a SQL query.\nSecond line is not shown in desc.",
            inputSchema={
                "type": "object",
                "properties": {"sql": {"type": "string", "description": "SQL text"}},
                "required": ["sql"],
            },
        ),
        Tool(name="list-schemas", description="", inputSchema={"type": "object"}),
    ],
}
PY_NAMES = {"postgres_lime": "postgres-lime"}


def test_name_detail_lists_refs_only():
    out = _search_tools_logic("", TOOLS, PY_NAMES, detail="name")
    assert "Connected MCP servers" in out  # пустой запрос — обзор при любом detail

    out = _search_tools_logic("sql", TOOLS, PY_NAMES, detail="name")
    assert "postgres_lime.execute_sql" in out
    assert "Run a SQL query" not in out
    assert "Args:" not in out and "await" not in out


def test_desc_detail_adds_first_description_line():
    out = _search_tools_logic("s", TOOLS, PY_NAMES, detail="desc")  # матчит оба тула
    assert "postgres_lime.execute_sql — Run a SQL query." in out
    assert "Second line" not in out
    assert "sql (str)" not in out
    # тул без описания — голая ссылка, без висящего тире
    assert "postgres_lime.list_schemas\n" in out + "\n"
    assert "list_schemas —" not in out


def test_full_detail_is_default_and_keeps_stubs():
    default = _search_tools_logic("sql", TOOLS, PY_NAMES)
    assert default == _search_tools_logic("sql", TOOLS, PY_NAMES, detail="full")
    assert "#   sql (str): SQL text" in default
    assert "result = await postgres_lime.execute_sql(sql: str)" in default


def test_detail_levels_grow_monotonically():
    levels = ("name", "desc", "full")
    sizes = [len(_search_tools_logic("sql", TOOLS, PY_NAMES, detail=d)) for d in levels]
    assert sizes == sorted(sizes) and len(set(sizes)) == 3


def test_unknown_detail_rejected():
    with pytest.raises(ValueError, match="detail must be one of"):
        _search_tools_logic("sql", TOOLS, PY_NAMES, detail="brief")
