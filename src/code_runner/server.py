"""
code-runner MCP server.

Exposes three tools to Claude:
  - list_available_tools  -> brief overview of connected servers
  - search_tools          -> keyword search returning full stubs
  - execute_code          -> run Python code with MCP tool access
"""

import logging
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import Tool

from .client_pool import MCPClientPool
from .schema_gen import generate_server_overview, generate_stubs_for_server
from .config_reader import server_name_to_py

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Servers to skip (avoid self-reference and heavy servers)
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


def _overview_logic(
    tools_by_server: dict[str, list[Tool]],
    py_name_map: dict[str, str],
) -> str:
    """Generate brief server overview. Extracted for testing."""
    return generate_server_overview(tools_by_server, py_name_map)


def _search_tools_logic(
    query: str,
    tools_by_server: dict[str, list[Tool]],
    py_name_map: dict[str, str],
) -> str:
    """Search tools by keyword. Extracted for testing."""
    if not query.strip():
        return _overview_logic(tools_by_server, py_name_map)

    keywords = query.lower().split()
    matches: dict[str, list[Tool]] = {}

    for server_name, tools in tools_by_server.items():
        for tool in tools:
            searchable = f"{tool.name} {tool.description or ''}".lower()
            if all(kw in searchable for kw in keywords):
                matches.setdefault(server_name, []).append(tool)

    if not matches:
        server_names = sorted(py_name_map.keys())
        return f"No tools found for '{query}'. Available servers: {', '.join(server_names)}"

    sections = []
    for server_name, tools in matches.items():
        py_name = server_name_to_py(server_name)
        sections.append(generate_stubs_for_server(py_name, tools))

    return "\n\n".join(sections)


@mcp.tool()
async def list_available_tools(ctx: Context) -> str:
    """
    List all connected MCP servers with tool counts.
    Returns a brief overview. Use search_tools(query) to find specific tools with full signatures.
    """
    pool: MCPClientPool = ctx.request_context.lifespan_context["pool"]
    py_name_map = pool.py_name_map()
    tools_by_server = pool.get_all_tools()

    if not py_name_map:
        return "No MCP servers connected."

    overview = _overview_logic(tools_by_server, py_name_map)

    if pool.failed:
        lines = ["", "# === Failed to connect ==="]
        for name, err in pool.failed.items():
            lines.append(f"# {name}: {err}")
        overview += "\n".join(lines)

    return overview


@mcp.tool()
async def search_tools(query: str, ctx: Context) -> str:
    """
    Search for MCP tools by keyword. Returns full Python stubs for matching tools.

    Args:
        query: Space-separated keywords. All keywords must match tool name or description.
               Examples: "sql query", "read file", "documentation"
    """
    pool: MCPClientPool = ctx.request_context.lifespan_context["pool"]
    return _search_tools_logic(query, pool.get_all_tools(), pool.py_name_map())


@mcp.tool()
async def execute_code(
    code: str,
    ctx: Context,
    timeout: float = 60.0,
    max_output_bytes: int = 20000,
) -> str:
    """
    Execute Python code with access to all connected MCP tools.

    Each MCP server is available as a Python object named after the server
    (hyphens replaced with underscores). Call tools using:
        result = await server_name.tool_name(param="value")

    Use list_available_tools first to discover servers, then
    search_tools to get full signatures for specific tools.

    Args:
        code: Python code to execute. Top-level await is supported.
        timeout: Maximum execution time in seconds (default 60).
        max_output_bytes: Max size of returned output in bytes (default 20000,
            ≈5K tokens). Output over this limit is truncated with a footer.
            Pass 0 to disable. Raise when you explicitly need a larger sample;
            prefer SQL LIMIT/TOP or pagination over bumping this.
    """
    from .executor import CodeExecutor

    pool: MCPClientPool = ctx.request_context.lifespan_context["pool"]
    executor = CodeExecutor(pool)

    result = await executor.execute(
        code,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
    )

    lines = []
    if result["output"]:
        lines.append(result["output"].rstrip())
    if not result["success"]:
        lines.append(f"\n[ERROR] {result['error']}")

    return "\n".join(lines) if lines else "(no output)"


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
