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
from .late_response import late_response_logger

logger = logging.getLogger(__name__)

CONNECTION_TIMEOUT = 30  # seconds per server
SHUTDOWN_TIMEOUT = 15  # seconds to wait for one server host to close


def _new_session(read, write, server_name: str) -> ClientSession:
    """Единственная точка создания downstream-сессии (stdio и http).

    Per-call таймаут бросает вызов, а downstream отвечает позже — такой ответ
    уходит в журнал warn, сессия остаётся рабочей."""
    return ClientSession(read, write, message_handler=late_response_logger(server_name))


class _ServerHost:
    """Owns one server's transport inside a dedicated task.

    anyio cancel-scopes принадлежат задаче, в которой были открыты, поэтому
    стек обязан закрываться в той же задаче. Прежняя версия обходила это тем,
    что не закрывала стек при reconnect вовсе, оставляя его на shutdown() пула.
    В stdio-режиме это было незаметно (пул умирал вместе с сессией), но в
    вечном демоне каждый reconnect навсегда оставлял живой stdio-процесс:
    1038 процессов / 47.8 GiB за 8.6 часов (02.08.2026). Тот же паттерн уже
    применён уровнем выше — `_PoolHost` в project_pools.py.
    """

    def __init__(self, name: str, opener):
        self.name = name
        self.session: ClientSession | None = None
        self.tools: list[Tool] = []
        self.error: BaseException | None = None
        self.ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(opener), name=f"mcp-server:{name}")

    async def _run(self, opener) -> None:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            self.session, self.tools = await opener(stack)
        except asyncio.CancelledError as e:
            # Отмену закрываем и пробрасываем: проглотить её значит превратить
            # снятие задачи в тихое «сервер не подключился».
            self.error = e
            await stack.aclose()
            self.ready.set()
            raise
        except Exception as e:  # noqa: BLE001 — ошибку подъёма отдаём владельцу
            self.error = e
            await stack.aclose()
            self.ready.set()
            return
        self.ready.set()
        try:
            await self._stop.wait()
        finally:
            await stack.aclose()

    async def stop(self) -> None:
        """Signal the host task to close its stack and wait for it.

        Через `asyncio.wait`, а не `await task`: ожидание не должно
        перевыбрасывать исключение задачи. Отменённый хост поднял бы
        CancelledError (это BaseException, `except Exception` его не ловит) и
        оборвал бы цикл в shutdown(), оставив остальные хосты неостановленными.
        По таймауту задачу не отменяем — пусть дозакрывается в фоне, иначе
        процесс снова осиротеет.
        """
        self._stop.set()
        done, _ = await asyncio.wait({self._task}, timeout=SHUTDOWN_TIMEOUT)
        if not done:
            logger.warning(
                f"Server host '{self.name}' did not close within {SHUTDOWN_TIMEOUT}s"
            )
            return
        exc = None if self._task.cancelled() else self._task.exception()
        if exc is not None:
            logger.warning(f"Server host stop error for '{self.name}': {exc}")


class MCPClientPool:
    def __init__(self):
        self._hosts: dict[str, _ServerHost] = {}
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
        """Start a host task for this server and publish its session.

        Транспорт открывается внутри задачи хоста — только так его можно
        потом закрыть (см. _ServerHost). Любой срыв ожидания, включая таймаут
        из _safe_connect, обязан погасить хост, иначе процесс осиротеет.
        """
        self.configs[name] = cfg
        opener = (
            self._http_opener(cfg)
            if cfg.transport == "http"
            else self._stdio_opener(cfg)
        )
        host = _ServerHost(name, opener)
        try:
            await host.ready.wait()
        except BaseException:
            await host.stop()
            raise
        if host.error is not None:
            await host.stop()
            raise host.error

        self._hosts[name] = host
        self.sessions[name] = host.session
        self.tools[name] = host.tools

    def _stdio_opener(self, cfg: ServerConfig):
        async def open_(stack: AsyncExitStack):
            merged_env = {**os.environ, **cfg.env}
            params = StdioServerParameters(
                command=cfg.command,
                args=cfg.args,
                env=merged_env,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(_new_session(read, write, cfg.name))
            await session.initialize()
            result = await session.list_tools()
            return session, result.tools

        return open_

    def _http_opener(self, cfg: ServerConfig):
        async def open_(stack: AsyncExitStack):
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

            session = await stack.enter_async_context(_new_session(read, write, cfg.name))
            await session.initialize()
            result = await session.list_tools()
            return session, result.tools

        return open_

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

        Гасит старый хост перед подъёмом нового: его стек закрывается в
        собственной задаче, поэтому anyio-ошибки "cancel scope in a different
        task" здесь не возникает, а старый stdio-процесс не остаётся жить."""
        cfg = self.configs.get(server_name)
        if cfg is None:
            raise RuntimeError(f"No stored config for '{server_name}' to reconnect")
        lock = self._reconnect_locks.setdefault(server_name, asyncio.Lock())
        async with lock:
            stale = self._hosts.pop(server_name, None)
            if stale is not None:
                await stale.stop()
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
        hosts = list(self._hosts.values())
        self._hosts.clear()
        self.sessions.clear()
        # Параллельно: последовательный цикл упирался бы в SHUTDOWN_TIMEOUT на
        # каждый зависший хост, а их десятки.
        await asyncio.gather(*(host.stop() for host in hosts))

    def get_all_tools(self) -> dict[str, list[Tool]]:
        return self.tools

    def connected_servers(self) -> list[str]:
        return list(self.sessions.keys())

    def py_name_map(self) -> dict[str, str]:
        """Map Python identifier -> original server name."""
        return {server_name_to_py(name): name for name in self.sessions}
