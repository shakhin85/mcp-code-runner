"""search_tools ранжирует по BM25: любое слово запроса может совпасть, лучшие — первыми."""

from mcp.types import Tool

from code_runner.server import SEARCH_LIMIT, _search_tools_logic


def _tool(name, description="", props=None):
    return Tool(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": props or {}},
    )


TOOLS = {
    "linear": [
        _tool("linear_searchIssues", "Search issues by text"),
        _tool("linear_createIssue", "Create a new issue in a team"),
    ],
    "postgres": [
        _tool("execute_sql", "Run a query", {"sql": {"type": "string", "description": "SQL text"}}),
    ],
    "filesystem": [_tool("read_text_file", "Read a text file from disk")],
}
PY = {"linear": "linear", "postgres": "postgres", "filesystem": "filesystem"}


def test_partial_match_still_found():
    # «ticket» нет ни в одном туле — AND-поиск вернул бы пусто
    out = _search_tools_logic("create issue ticket", TOOLS, PY, detail="name")
    assert "linear.linear_createIssue" in out


def test_best_match_first():
    out = _search_tools_logic("create issue", TOOLS, PY, detail="name")
    assert out.index("linear_createIssue") < out.index("linear_searchIssues")


def test_camel_case_name_is_tokenized():
    out = _search_tools_logic("issues", TOOLS, PY, detail="name")
    assert "linear.linear_searchIssues" in out


def test_parameter_description_is_searched():
    out = _search_tools_logic("text", TOOLS, PY, detail="name")
    assert "postgres.execute_sql" in out


def test_prefix_matches():
    out = _search_tools_logic("quer", TOOLS, PY, detail="name")
    assert "postgres.execute_sql" in out


def test_server_sections_follow_best_score():
    out = _search_tools_logic("text file", TOOLS, PY, detail="name")
    assert out.index("filesystem") < out.index("postgres")


def test_limit_and_footer():
    many = {"srv": [_tool(f"tool_{i}", "fetch data") for i in range(SEARCH_LIMIT + 5)]}
    out = _search_tools_logic("fetch", many, {"srv": "srv"}, detail="name")
    assert out.count("srv.tool_") == SEARCH_LIMIT
    assert f"{SEARCH_LIMIT} of {SEARCH_LIMIT + 5}" in out
