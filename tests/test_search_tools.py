from mcp.types import Tool

from code_runner.server import _search_tools_logic, _overview_logic


def _make_tool(name, description="", schema=None):
    return Tool(name=name, description=description or name, inputSchema=schema or {"type": "object", "properties": {}})


TOOLS_BY_SERVER = {
    "mssql": [_make_tool("execute_sql", "Execute a SQL query against the database")],
    "filesystem": [
        _make_tool("read_text_file", "Read a text file from disk"),
        _make_tool("write_file", "Write content to a file"),
    ],
    "context7": [_make_tool("query_docs", "Query documentation for a library")],
}

PY_NAME_MAP = {"mssql": "mssql", "filesystem": "filesystem", "context7": "context7"}


class TestSearchTools:
    def test_single_keyword(self):
        results = _search_tools_logic("sql", TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "execute_sql" in results

    def test_multi_keyword_and(self):
        results = _search_tools_logic("text file", TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "read_text_file" in results
        assert "execute_sql" not in results

    def test_no_match(self):
        results = _search_tools_logic("blockchain", TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "No tools found" in results

    def test_empty_query_returns_overview(self):
        results = _search_tools_logic("", TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "Connected MCP servers" in results

    def test_whitespace_query_returns_overview(self):
        results = _search_tools_logic("   ", TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "Connected MCP servers" in results

    def test_single_word_matches_description(self):
        results = _search_tools_logic("database", TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "execute_sql" in results


class TestOverview:
    def test_shows_server_names(self):
        overview = _overview_logic(TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "mssql" in overview
        assert "filesystem" in overview
        assert "context7" in overview

    def test_shows_tool_counts(self):
        overview = _overview_logic(TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "2 tools" in overview  # filesystem

    def test_does_not_show_full_stubs(self):
        overview = _overview_logic(TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "result = await" not in overview
