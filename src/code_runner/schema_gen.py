"""
Converts MCP tool schemas into Python function stubs for LLM consumption.
"""

from mcp.types import Tool


_JSON_TYPE_MAP = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
    "null": "None",
}


def json_type_to_py(schema: dict) -> str:
    t = schema.get("type")
    if isinstance(t, list):
        types = [_JSON_TYPE_MAP.get(x, "Any") for x in t if x != "null"]
        nullable = "null" in t
        result = " | ".join(types) if types else "Any"
        return f"{result} | None" if nullable else result
    return _JSON_TYPE_MAP.get(t, "Any")


def tool_to_stub(server_py_name: str, tool: Tool) -> str:
    """Generate a Python async function stub string."""
    props: dict = {}
    required: list[str] = []

    if tool.inputSchema:
        props = tool.inputSchema.get("properties", {})
        required = tool.inputSchema.get("required", [])

    params = []
    for param_name, param_schema in props.items():
        py_type = json_type_to_py(param_schema)
        if param_name not in required:
            params.append(f"{param_name}: {py_type} = None")
        else:
            params.append(f"{param_name}: {py_type}")

    params_str = ", ".join(params)
    desc = (tool.description or "").replace("\n", " ").strip()
    if len(desc) > 120:
        desc = desc[:117] + "..."

    lines = [
        f"# {desc}",
        f"result = await {server_py_name}.{tool.name}({params_str})",
    ]
    return "\n".join(lines)


def generate_stubs_for_server(server_py_name: str, tools: list[Tool]) -> str:
    """Generate all stubs for a single server."""
    if not tools:
        return f"# {server_py_name}: no tools available"

    lines = [f"# === {server_py_name} ({len(tools)} tools) ==="]
    for tool in tools:
        lines.append(tool_to_stub(server_py_name, tool))
        lines.append("")
    return "\n".join(lines)


def generate_full_reference(tools_by_server: dict[str, list[Tool]], py_name_map: dict[str, str]) -> str:
    """Generate complete Python reference for all connected servers."""
    sections = []
    for py_name, server_name in sorted(py_name_map.items()):
        tools = tools_by_server.get(server_name, [])
        sections.append(generate_stubs_for_server(py_name, tools))
    return "\n\n".join(sections)
