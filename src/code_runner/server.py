"""
code-runner MCP server.

Exposes three tools to Claude:
  - list_available_tools  -> brief overview of connected servers
  - search_tools          -> keyword search returning full stubs
  - execute_code          -> run Python code with MCP tool access
"""

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from weakref import WeakKeyDictionary

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import Tool

from .client_pool import MCPClientPool
from .config_reader import load_server_configs, server_name_to_py
from .executor import CodeExecutor
from .metrics import recorder_from_env, summarize_errors
from .project_pools import STARTUP_TIMEOUT, ProjectPoolRegistry, _PoolHost
from .schema_gen import generate_server_overview, generate_stubs_for_server
from .skills import SkillLoader, SkillsNamespace, SkillSpec, write_skill_files

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Servers to skip: self-reference + heavy stdio servers that don't bridge cleanly
# (e.g. 1С servers — their subprocess dies with ClosedResourceError under the pool).
# Extend via CODE_RUNNER_SKIP_SERVERS env (comma-separated server names).
SKIP_SERVERS: set[str] = {"code-runner", "serena"} | {
    s.strip()
    for s in os.environ.get("CODE_RUNNER_SKIP_SERVERS", "").split(",")
    if s.strip()
}

SKILLS_DIR = Path.home() / ".claude" / "code-runner-skills"


def _build_state(pool: MCPClientPool, project_pools) -> dict[str, Any]:
    logger.info(f"Connected: {pool.connected_servers()}")
    if pool.failed:
        logger.warning(f"Failed: {pool.failed}")

    recorder = recorder_from_env()
    if recorder is not None:
        logger.info(f"Metrics enabled: {recorder.path}")

    # Long-lived executor so persistent session namespaces survive across
    # execute_code calls within the same server process.
    loader = SkillLoader(SKILLS_DIR)
    skills_ns = SkillsNamespace(loader.discover())
    executor = CodeExecutor(pool, recorder=recorder, skills=skills_ns)
    return {
        "pool": pool,
        "executor": executor,
        "skills_loader": loader,
        "project_pools": project_pools,
    }


# Daemon mode: FastMCP enters `lifespan` once per MCP session, not once per
# process. Building the pools there spawned a full copy of every global server
# per session (TasksMax exhaustion, zero evictions — each session had its own
# registry). State is built once, the global pool hosted in a session-independent
# task (anyio cancel scopes die with the first session's task otherwise), and
# never torn down per-session: process exit kills the cgroup.
_daemon_state: dict[str, Any] | None = None
_daemon_lock = asyncio.Lock()


async def _daemon_state_singleton() -> dict[str, Any]:
    global _daemon_state
    async with _daemon_lock:
        if _daemon_state is None:
            host = _PoolHost("global", load_server_configs(SKIP_SERVERS))
            try:
                await asyncio.wait_for(host.ready.wait(), timeout=STARTUP_TIMEOUT)
            except TimeoutError:
                # Tear the host down before propagating, or the orphaned task
                # would duplicate the pool when the next session retries.
                await host.stop()
                raise
            registry = ProjectPoolRegistry(
                global_names=set(host.pool.configs), skip_servers=SKIP_SERVERS
            )
            state = _build_state(host.pool, registry)
            state["_pool_host"] = host
            _daemon_state = state
    return _daemon_state


@asynccontextmanager
async def lifespan(server: FastMCP):
    if os.environ.get("CODE_RUNNER_TRANSPORT") == "streamable-http":
        yield await _daemon_state_singleton()
        return

    # stdio: one lifespan per process owns the pool and tears it down.
    # Проектные серверы уже смержены в global pool (project_pools не нужен).
    pool = MCPClientPool()
    await pool.startup(skip_servers=SKIP_SERVERS)
    try:
        yield _build_state(pool, None)
    finally:
        await pool.shutdown()


mcp = FastMCP("code-runner", lifespan=lifespan)

# client session -> resolved project dir (or None if roots недоступны).
# Roots запрашиваются у клиента один раз на MCP-сессию.
_session_roots: "WeakKeyDictionary[Any, Path | None]" = WeakKeyDictionary()


async def _resolve_project_dir(ctx: Context) -> "Path | None":
    session = ctx.session
    if session in _session_roots:
        return _session_roots[session]
    project_dir: Path | None = None
    try:
        result = await ctx.session.list_roots()
        for root in result.roots:
            parsed = urlparse(str(root.uri))
            if parsed.scheme == "file" and parsed.path:
                candidate = Path(unquote(parsed.path))
                if candidate.is_dir():
                    project_dir = candidate
                    break
    except Exception as e:
        logger.debug(f"list_roots unavailable: {e}")
    _session_roots[session] = project_dir
    return project_dir


async def _project_pool(ctx: Context) -> "MCPClientPool | None":
    """Per-project pool for this client session (daemon mode), else None."""
    registry: ProjectPoolRegistry | None = ctx.request_context.lifespan_context.get(
        "project_pools"
    )
    if registry is None:
        return None
    project_dir = await _resolve_project_dir(ctx)
    if project_dir is None:
        return None
    try:
        return await registry.get(project_dir)
    except Exception as e:
        logger.warning(f"Project pool unavailable for {project_dir}: {e}")
        return None


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


def _format_skills_section(specs: dict[str, SkillSpec]) -> str:
    """Render the '# === Skills ===' section for list_available_tools.

    Returns an empty string when there are no skills so the caller
    can unconditionally append.
    """
    if not specs:
        return ""
    lines = [
        "",
        "# === Skills ===",
        "# (call as skills.<name>.<fn>(...))",
    ]
    for name in sorted(specs):
        desc = (specs[name].description or "").strip()
        line = f"# - skills.{name}"
        if desc:
            line += f": {desc}"
        lines.append(line)
    return "\n".join(lines)


async def _merged_views(
    ctx: Context, pool: MCPClientPool
) -> tuple[dict[str, str], dict[str, list[Tool]]]:
    """Global + project-pool views as COPIES (pool internals must stay clean)."""
    py_name_map = dict(pool.py_name_map())
    tools_by_server = dict(pool.get_all_tools())
    extra = await _project_pool(ctx)
    if extra is not None:
        for server_name, tools in extra.get_all_tools().items():
            tools_by_server.setdefault(server_name, tools)
        for py_name, server_name in extra.py_name_map().items():
            py_name_map.setdefault(py_name, server_name)
    return py_name_map, tools_by_server


@mcp.tool()
async def list_available_tools(ctx: Context) -> str:
    """
    List all connected MCP servers with tool counts.
    Returns a brief overview. Use search_tools(query) to find specific tools with full signatures.
    """
    pool: MCPClientPool = ctx.request_context.lifespan_context["pool"]
    py_name_map, tools_by_server = await _merged_views(ctx, pool)

    if not py_name_map:
        return "No MCP servers connected."

    overview = _overview_logic(tools_by_server, py_name_map)

    if pool.failed:
        lines = ["", "# === Failed to connect ==="]
        for name, err in pool.failed.items():
            lines.append(f"# {name}: {err}")
        overview += "\n".join(lines)

    loader = ctx.request_context.lifespan_context.get("skills_loader")
    if loader is not None:
        overview += _format_skills_section(loader.discover())

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
    py_name_map, tools_by_server = await _merged_views(ctx, pool)
    return _search_tools_logic(query, tools_by_server, py_name_map)


@mcp.tool()
async def execute_code(
    code: str,
    ctx: Context,
    timeout: float = 60.0,
    max_output_bytes: int = 20000,
    session_id: str | None = None,
    auto_limit: int = 500,
    raw_cap: int = 262144,
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
        raw_cap: Per-single-tool-call byte cap (default 262144 ≈ 256KB).
            auto_limit caps ROWS; this caps BYTES, so a `SELECT * LIMIT 500`
            over wide/jsonb columns can't still pull megabytes. When one tool
            response exceeds raw_cap it is returned as a TRUNCATED STRING (not
            parsed into dicts/lists) with a marker — narrow columns or aggregate
            and re-run. Use print_rows(result) for a compact head/tail preview
            instead of print(result). Pass 0 to disable.
    """
    executor: CodeExecutor = ctx.request_context.lifespan_context["executor"]

    result = await executor.execute(
        code,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        session_id=session_id,
        auto_limit=auto_limit,
        raw_cap=raw_cap,
        extra_pool=await _project_pool(ctx),
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
    summary: bool = False,
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
        summary: Return a failure breakdown of execute_code runs instead of the
            events themselves: runs/failed/fail_rate, counts per error class
            (SyntaxError, BlockedImport, NameError, Timeout, ...) and
            syntax_share. Pass a wide `since` and a large `limit` — the
            aggregate is what comes back, not the events. A rising
            syntax_share means calls are being routed into the sandbox that
            were small enough to be direct tool calls.

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
    if summary:
        return json.dumps(
            summarize_errors(events), ensure_ascii=False, default=str, indent=2
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


@mcp.tool()
async def debug_roots(ctx: Context) -> str:
    """TEMP: report MCP roots the client exposes (per-session project dir probe)."""
    try:
        result = await ctx.session.list_roots()
        return json.dumps([str(r.uri) for r in result.roots])
    except Exception as e:
        return f"list_roots failed: {type(e).__name__}: {e}"


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")

    # CODE_RUNNER_TRANSPORT=streamable-http поднимает HTTP-демон на
    # FASTMCP_HOST/FASTMCP_PORT; конструктор FastMCP перекрывает env-Settings
    # своими дефолтами (host=127.0.0.1, port=8000), поэтому выставляем явно.
    transport = os.environ.get("CODE_RUNNER_TRANSPORT", "stdio")
    if host := os.environ.get("FASTMCP_HOST"):
        mcp.settings.host = host
    if port := os.environ.get("FASTMCP_PORT"):
        mcp.settings.port = int(port)
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
