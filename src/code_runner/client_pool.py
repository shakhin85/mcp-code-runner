"""
Manages long-lived connections to all configured MCP servers.
Acts as MCP client to each server.
"""

import asyncio
import logging
import os
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import Tool

from .config_reader import ServerConfig, load_server_configs, server_name_to_py

logger = logging.getLogger(__name__)

CONNECTION_TIMEOUT = 30  # seconds per server


class MCPClientPool:
    def __init__(self):
        self._exit_stack = AsyncExitStack()
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, list[Tool]] = {}
        self.failed: dict[str, str] = {}
        # Keep configs so a dead session (e.g. HTTP server restarted and the
        # cached Mcp-Session-Id got invalidated) can be re-established on demand.
        self.configs: dict[str, ServerConfig] = {}
        self._reconnect_locks: dict[str, asyncio.Lock] = {}

    async def startup(
        self,
        skip_servers: set[str] | None = None,
        configs: dict[str, ServerConfig] | None = None,
    ) -> None:
        """Connect to all configured MCP servers in parallel.

        configs: explicit server set (per-project pools in daemon mode);
        default — read from ~/.claude.json + project configs.
        """
        await self._exit_stack.__aenter__()
        if configs is None:
            configs = load_server_configs(skip_servers)

        tasks = [
            self._safe_connect(name, cfg)
            for name, cfg in configs.items()
        ]
        await asyncio.gather(*tasks)

    async def _safe_connect(self, name: str, cfg: ServerConfig) -> None:
        try:
            await asyncio.wait_for(
                self._connect(name, cfg),
                timeout=CONNECTION_TIMEOUT,
            )
            tool_count = len(self.tools.get(name, []))
            logger.info(f"Connected to '{name}' ({tool_count} tools)")
        except Exception as e:
            self.failed[name] = str(e)
            logger.warning(f"Failed to connect to '{name}': {e}")

    async def _connect(self, name: str, cfg: ServerConfig) -> None:
        self.configs[name] = cfg
        if cfg.transport == "http":
            await self._connect_http(name, cfg)
        else:
            await self._connect_stdio(name, cfg)

    async def _connect_stdio(self, name: str, cfg: ServerConfig) -> None:
        merged_env = {**os.environ, **cfg.env}
        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args,
            env=merged_env,
        )

        stack = AsyncExitStack()
        await stack.__aenter__()

        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            result = await session.list_tools()
            self.sessions[name] = session
            self.tools[name] = result.tools

            self._exit_stack.push_async_callback(stack.aclose)
        except BaseException:
            await stack.aclose()
            raise

    async def _connect_http(self, name: str, cfg: ServerConfig) -> None:
        """Connect to HTTP/SSE MCP server using an isolated exit stack."""
        stack = AsyncExitStack()
        await stack.__aenter__()

        try:
            try:
                from mcp.client.streamable_http import streamablehttp_client

                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(cfg.url, headers=cfg.env or {})
                )
            except ImportError:
                from mcp.client.sse import sse_client

                read, write = await stack.enter_async_context(
                    sse_client(cfg.url)
                )

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            result = await session.list_tools()
            self.sessions[name] = session
            self.tools[name] = result.tools

            self._exit_stack.push_async_callback(stack.aclose)
        except BaseException:
            await stack.aclose()
            raise

    # Substrings that mark a dead transport/session (vs. a legit tool error)
    # worth a single reconnect + retry.
    _RECONNECTABLE = (
        "session terminated",
        "session not found",
        "closedresource",
        "connection",
        "broken pipe",
        "peer closed",
        "http 404",
    )

    def _is_reconnectable(self, exc: Exception) -> bool:
        # Класс в текст матчинга включён намеренно: anyio.ClosedResourceError
        # приходит с ПУСТЫМ сообщением, поэтому по str(exc) маркер
        # "closedresource" не находился никогда и реконнект не срабатывал —
        # ровно на 1С-серверах, ради которых он и писался.
        msg = f"{type(exc).__name__} {exc}".lower()
        return any(marker in msg for marker in self._RECONNECTABLE)

    async def _reconnect(self, server_name: str) -> None:
        """Re-establish a single server's session (e.g. after it restarted).

        Drops the stale session and connects fresh. The old per-server exit
        stack is left for shutdown() to close — releasing it here would risk
        anyio's "cancel scope in a different task" since it was entered under a
        different startup task."""
        cfg = self.configs.get(server_name)
        if cfg is None:
            raise RuntimeError(f"No stored config for '{server_name}' to reconnect")
        lock = self._reconnect_locks.setdefault(server_name, asyncio.Lock())
        async with lock:
            await self._connect(server_name, cfg)

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict):
        session = self.sessions.get(server_name)
        if session is None:
            raise RuntimeError(f"Server '{server_name}' is not connected")
        try:
            return await session.call_tool(tool_name, arguments)
        except Exception as e:
            if not self._is_reconnectable(e):
                raise
            logger.warning(
                f"'{server_name}' session dead ({e}); reconnecting and retrying once"
            )
            await self._reconnect(server_name)
            session = self.sessions.get(server_name)
            if session is None:
                raise RuntimeError(f"Reconnect to '{server_name}' failed") from e
            return await session.call_tool(tool_name, arguments)

    async def shutdown(self) -> None:
        await self._exit_stack.aclose()

    def get_all_tools(self) -> dict[str, list[Tool]]:
        return self.tools

    def connected_servers(self) -> list[str]:
        return list(self.sessions.keys())

    def py_name_map(self) -> dict[str, str]:
        """Map Python identifier -> original server name."""
        return {server_name_to_py(name): name for name in self.sessions}
