"""
code-runner MCP server.

Exposes three tools to Claude:
  - list_available_tools  -> brief overview of connected servers
  - search_tools          -> keyword search returning full stubs
  - execute_code          -> run Python code with MCP tool access
"""

import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import Tool

from .client_pool import MCPClientPool
from .executor import CodeExecutor
from .metrics import recorder_from_env
from .schema_gen import generate_server_overview, generate_stubs_for_server
from .config_reader import server_name_to_py
from .skills import SkillLoader, SkillsNamespace, write_skill_files

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Servers to skip (avoid self-reference and heavy servers)
SKIP_SERVERS: set[str] = {"code-runner", "serena"}

SKILLS_DIR = Path.home() / ".claude" / "code-runner-skills"


@asynccontextmanager
async def lifespan(server: FastMCP):
    pool = MCPClientPool()
    await pool.startup(skip_servers=SKIP_SERVERS)

    connected = pool.connected_servers()
    failed = pool.failed

    logger.info(f"Connected: {connected}")
    if failed:
        logger.warning(f"Failed: {failed}")

    recorder = recorder_from_env()
    if recorder is not None:
        logger.info(f"Metrics enabled: {recorder.path}")

    # Long-lived executor so persistent session namespaces survive across
    # execute_code calls within the same server process.
    loader = SkillLoader(SKILLS_DIR)
    skills_ns = SkillsNamespace(loader.discover())
    executor = CodeExecutor(pool, recorder=recorder, skills=skills_ns)

    try:
        yield {
            "pool": pool,
            "executor": executor,
            "skills_loader": loader,
        }
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
    session_id: str | None = None,
    auto_limit: int = 500,
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
        session_id: Optional string id. When two calls share the same id,
            user-defined variables (assignments) persist between them, so a
            follow-up call can reuse fetched data without re-running the
            MCP query. Idle sessions expire after ~10 minutes; ≤20 sessions
            are kept at once (LRU eviction). Omit for one-shot execution.
        auto_limit: Default row cap applied to bare SELECT queries sent to
            postgres_*/mssql execute_sql tools (default 500). The proxy
            rewrites `SELECT ... FROM t` into `SELECT ... FROM t LIMIT 500`
            (or `SELECT TOP 500 ...` for MSSQL) when the user hasn't set
            their own LIMIT/TOP. INSERT/UPDATE/DELETE/DDL are never rewritten.
            Pass 0 to disable entirely, or a larger value when you really
            need more rows (combine with max_output_bytes).
    """
    executor: CodeExecutor = ctx.request_context.lifespan_context["executor"]

    result = await executor.execute(
        code,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        session_id=session_id,
        auto_limit=auto_limit,
    )

    lines = []
    if result["output"]:
        lines.append(result["output"].rstrip())
    if not result["success"]:
        lines.append(f"\n[ERROR] {result['error']}")

    return "\n".join(lines) if lines else "(no output)"


@mcp.tool()
async def get_metrics(
    ctx: Context,
    since: str | None = None,
    server: str | None = None,
    kind: str | None = None,
    limit: int = 100,
) -> str:
    """
    Return recent code-runner metrics events as a JSON list (most recent last).

    Two event kinds are recorded:
      - "tool_call": one per MCP tool invocation (server, tool, duration_ms,
        bytes, success, limit_applied, error).
      - "execute_code": one per execute_code call — a rollup with total
        duration, output_bytes_raw/sent, truncated flag, tool_calls count,
        auto_limit_hits count, session_id.

    Args:
        since: ISO-8601 timestamp (e.g. "2026-04-17T10:00:00Z"); only events
            with ts >= since are returned. String comparison, no parsing.
        server: Filter tool_call events to a specific server name (e.g. "mssql").
        kind: "tool_call" or "execute_code". Omit for both.
        limit: Max events (default 100). Oldest trimmed first.

    Returns JSON string. Empty list if no events match or metrics are disabled.
    """
    executor: CodeExecutor = ctx.request_context.lifespan_context["executor"]
    if executor.recorder is None:
        return json.dumps(
            {"error": "metrics disabled — set CODE_RUNNER_METRICS=1 and restart"}
        )
    events = executor.recorder.read(
        since=since, server=server, kind=kind, limit=limit
    )
    return json.dumps(events, ensure_ascii=False, default=str, indent=2)


@mcp.tool()
async def save_skill(name: str, code: str, description: str, ctx: Context) -> str:
    """
    Save a skill to ~/.claude/code-runner-skills/<name>/.

    A skill is a Python file plus a description. Once saved, its public
    functions are immediately available inside execute_code as
    skills.<name>.<function_name>(...). Skills are local and trusted —
    they run with full Python builtins, can import packages from this
    server's venv, and are persistent across restarts.

    Overwriting an existing skill of the same name is allowed.

    Args:
        name: lowercase alphanumeric + underscore, max 40 chars,
            must start with a letter (matches ^[a-z][a-z0-9_]{0,39}$).
        code: full Python source for script.py.
        description: one-sentence summary used in list_available_tools.
    """
    target = write_skill_files(SKILLS_DIR, name, code, description)

    loader: SkillLoader = ctx.request_context.lifespan_context["skills_loader"]
    new_ns = SkillsNamespace(loader.discover())
    executor: CodeExecutor = ctx.request_context.lifespan_context["executor"]
    executor.skills = new_ns

    return f"Saved skill {name!r} to {target}"


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
