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

    async def startup(self, skip_servers: set[str] | None = None) -> None:
        """Connect to all configured MCP servers in parallel."""
        await self._exit_stack.__aenter__()
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

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict):
        session = self.sessions.get(server_name)
        if session is None:
            raise RuntimeError(f"Server '{server_name}' is not connected")
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
