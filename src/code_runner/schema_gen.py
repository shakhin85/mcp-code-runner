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


def _describe_object_fields(schema: dict) -> str:
    """Render object properties as {key: type, ...} one level deep."""
    props = schema.get("properties", {})
    if not props:
        return "dict"
    fields = []
    for k, v in props.items():
        fields.append(f"{k}: {json_type_to_py(v)}")
    return "{" + ", ".join(fields) + "}"


def _param_line(name: str, schema: dict, is_required: bool) -> tuple[str, str]:
    """Return (signature_part, doc_line) for a parameter."""
    py_type = json_type_to_py(schema)
    desc = schema.get("description", "")

    # Enum values
    enum = schema.get("enum")
    if enum:
        enum_str = ", ".join(f'"{v}"' if isinstance(v, str) else str(v) for v in enum)
        desc = f"{desc}. Values: {enum_str}" if desc else f"Values: {enum_str}"

    # Nested object detail
    if schema.get("type") == "object" and schema.get("properties"):
        obj_detail = _describe_object_fields(schema)
        desc = f"{desc}. Structure: {obj_detail}" if desc else f"Structure: {obj_detail}"

    optional_tag = "" if is_required else ", optional"
    doc = f"#   {name} ({py_type}{optional_tag}): {desc}" if desc else f"#   {name} ({py_type}{optional_tag})"

    sig = f"{name}: {py_type}" if is_required else f"{name}: {py_type} = None"

    return sig, doc


def tool_to_stub(server_py_name: str, tool: Tool) -> str:
    """Generate a Python async function stub string with docs."""
    props: dict = {}
    required: list[str] = []

    if tool.inputSchema:
        props = tool.inputSchema.get("properties", {})
        required = tool.inputSchema.get("required", [])

    sig_parts = []
    doc_lines = []

    for param_name, param_schema in props.items():
        sig, doc = _param_line(param_name, param_schema, param_name in required)
        sig_parts.append(sig)
        doc_lines.append(doc)

    params_str = ", ".join(sig_parts)
    desc = (tool.description or "").strip()
    if len(desc) > 200:
        desc = desc[:197] + "..."

    lines = [f"# {desc}"]
    if doc_lines:
        lines.append("# Args:")
        lines.extend(doc_lines)
    lines.append(f"result = await {server_py_name}.{tool.name}({params_str})")

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


def generate_server_overview(tools_by_server: dict[str, list[Tool]], py_name_map: dict[str, str]) -> str:
    """Generate brief overview of connected servers (names + tool counts)."""
    lines = ["# Connected MCP servers:"]
    for py_name, server_name in sorted(py_name_map.items()):
        tools = tools_by_server.get(server_name, [])
        count = len(tools)
        desc = ""
        if tools and tools[0].description:
            desc = tools[0].description.split("\n")[0].strip()
            if len(desc) > 80:
                desc = desc[:77] + "..."
        line = f"# - {py_name} ({count} tools)"
        if desc:
            line += f": {desc}"
        lines.append(line)
    lines.append('#')
    lines.append('# Use search_tools(query="...") to find specific tools with full signatures.')
    return "\n".join(lines)
