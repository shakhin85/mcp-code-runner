"""
code-runner MCP server.

Exposes three tools to Claude:
  - list_available_tools  → discover all tools across all MCP servers
  - execute_code          → run Python code with MCP tool access
  - get_tool_stub         → get usage example for a specific tool
"""

import json
import logging
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import Context, FastMCP

from .client_pool import MCPClientPool
from .executor import CodeExecutor
from .schema_gen import generate_full_reference, generate_stubs_for_server, tool_to_stub
from .config_reader import server_name_to_py

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Servers to skip when connecting (avoid self-reference and heavy servers that rarely needed)
SKIP_SERVERS: set[str] = {"code-runner", "serena"}


@asynccontextmanager
async def lifespan(server: FastMCP):
    pool = MCPClientPool()
    await pool.startup(skip_servers=SKIP_SERVERS)

    connected = pool.connected_servers()
    failed = pool.failed

    logger.info(f"Connected: {connected}")
    if failed:
        logger.warning(f"Failed: {failed}")

    try:
        yield {"pool": pool}
    finally:
        await pool.shutdown()


mcp = FastMCP("code-runner", lifespan=lifespan)


@mcp.tool()
async def list_available_tools(ctx: Context) -> str:
    """
    List all tools available across all connected MCP servers.
    Returns Python-style stubs showing how to call each tool in execute_code.
    """
    pool: MCPClientPool = ctx.request_context.lifespan_context["pool"]

    py_name_map = pool.py_name_map()
    tools_by_server = pool.get_all_tools()

    if not py_name_map:
        return "No MCP servers connected."

    reference = generate_full_reference(tools_by_server, py_name_map)

    failed_info = ""
    if pool.failed:
        lines = ["", "# === Failed to connect ==="]
        for name, err in pool.failed.items():
            lines.append(f"# {name}: {err}")
        failed_info = "\n".join(lines)

    return reference + failed_info


@mcp.tool()
async def execute_code(code: str, ctx: Context, timeout: float = 60.0) -> str:
    """
    Execute Python code with access to all connected MCP tools.

    Each MCP server is available as a Python object named after the server
    (hyphens replaced with underscores). Call tools using:
        result = await server_name.tool_name(param="value")

    Use list_available_tools first to see what's available.

    Args:
        code: Python code to execute. Top-level await is supported.
        timeout: Maximum execution time in seconds (default 60).
    """
    pool: MCPClientPool = ctx.request_context.lifespan_context["pool"]
    executor = CodeExecutor(pool)

    result = await executor.execute(code, timeout=timeout)

    lines = []
    if result["output"]:
        lines.append(result["output"].rstrip())
    if not result["success"]:
        lines.append(f"\n[ERROR] {result['error']}")

    return "\n".join(lines) if lines else "(no output)"


@mcp.tool()
async def get_tool_stub(server_name: str, tool_name: str, ctx: Context) -> str:
    """
    Get detailed usage stub for a specific tool.

    Args:
        server_name: Server name as shown in list_available_tools (e.g. 'mssql', 'filesystem')
        tool_name: Exact tool name (e.g. 'execute_sql', 'read_text_file')
    """
    pool: MCPClientPool = ctx.request_context.lifespan_context["pool"]

    # Accept both original and Python-style names
    py_map = pool.py_name_map()  # py_name -> original
    original_name = py_map.get(server_name, server_name)

    tools = pool.tools.get(original_name, [])
    tool = next((t for t in tools if t.name == tool_name), None)

    if tool is None:
        available = [t.name for t in tools]
        return f"Tool '{tool_name}' not found in '{server_name}'. Available: {available}"

    py_name = server_name_to_py(original_name)
    stub = tool_to_stub(py_name, tool)

    # Also include full schema
    schema_str = json.dumps(tool.inputSchema, indent=2, ensure_ascii=False) if tool.inputSchema else "{}"
    return f"{stub}\n\nFull schema:\n{schema_str}"


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
