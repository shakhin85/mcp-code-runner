from mcp.types import Tool

from code_runner.schema_gen import tool_to_stub, generate_server_overview


def _make_tool(name: str, description: str, schema: dict) -> Tool:
    return Tool(name=name, description=description, inputSchema=schema)


class TestToolToStub:
    def test_param_description_included(self):
        tool = _make_tool("execute_sql", "Execute a SQL query", {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The SQL query to execute",
                },
            },
            "required": ["query"],
        })
        stub = tool_to_stub("mssql", tool)
        assert "The SQL query to execute" in stub

    def test_enum_values_shown(self):
        tool = _make_tool("get_candles", "Get candle data", {
            "type": "object",
            "properties": {
                "interval": {
                    "type": "string",
                    "description": "Candle interval",
                    "enum": ["1m", "5m", "1h", "1d"],
                },
            },
            "required": ["interval"],
        })
        stub = tool_to_stub("crypto", tool)
        assert "1m" in stub
        assert "1d" in stub

    def test_nested_object_shown(self):
        tool = _make_tool("create", "Create item", {
            "type": "object",
            "properties": {
                "options": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "enabled": {"type": "boolean"},
                    },
                },
            },
            "required": [],
        })
        stub = tool_to_stub("srv", tool)
        assert "key" in stub
        assert "enabled" in stub

    def test_optional_param_marked(self):
        tool = _make_tool("foo", "Do foo", {
            "type": "object",
            "properties": {
                "required_param": {"type": "string"},
                "optional_param": {"type": "integer"},
            },
            "required": ["required_param"],
        })
        stub = tool_to_stub("srv", tool)
        assert "optional" in stub.lower()
