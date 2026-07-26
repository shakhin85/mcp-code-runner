"""Reconnect-on-dead-session behaviour for MCPClientPool.call_tool.

Regression: an HTTP MCP server (e.g. forgetful) restarts, invalidating the
cached Mcp-Session-Id. Every subsequent call_tool used to raise
"McpError: Session terminated" forever because the session was cached once at
startup with no reconnect. call_tool now re-establishes the session once and
retries on transport-level failures.
"""

import asyncio

import pytest

from code_runner.client_pool import MCPClientPool
from code_runner.config_reader import ServerConfig


class _Session:
    def __init__(self, *, fail_with=None, payload="ok"):
        self.fail_with = fail_with
        self.payload = payload
        self.calls = 0

    async def call_tool(self, name, arguments):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.payload


def _pool_with(session):
    pool = MCPClientPool()
    pool.configs["srv"] = ServerConfig(name="srv", transport="http", url="http://x/mcp")
    pool.sessions["srv"] = session
    return pool


def test_reconnects_and_retries_on_session_terminated():
    dead = _Session(fail_with=RuntimeError("McpError: Session terminated"))
    fresh = _Session(payload="recovered")
    pool = _pool_with(dead)

    async def fake_connect(name, cfg):
        pool.sessions[name] = fresh

    pool._connect = fake_connect  # type: ignore[assignment]

    result = asyncio.run(pool.call_tool("srv", "query_memory", {}))
    assert result == "recovered"
    assert dead.calls == 1  # failed once
    assert fresh.calls == 1  # retried once on the fresh session


def test_reconnects_on_empty_message_closed_resource():
    """anyio.ClosedResourceError несёт ПУСТОЕ сообщение — маркер живёт только
    в имени класса. Матчинг по одному str(exc) не ловил его никогда, из-за чего
    1С-серверы навсегда отваливались после первого обрыва вместо реконнекта."""
    import anyio

    dead = _Session(fail_with=anyio.ClosedResourceError())
    fresh = _Session(payload="recovered")
    pool = _pool_with(dead)

    async def fake_connect(name, cfg):
        pool.sessions[name] = fresh

    pool._connect = fake_connect  # type: ignore[assignment]

    assert str(dead.fail_with) == ""  # предпосылка бага
    assert asyncio.run(pool.call_tool("srv", "execute_query", {})) == "recovered"
    assert fresh.calls == 1


def test_non_transport_error_propagates_without_reconnect():
    bad = _Session(fail_with=ValueError("validation: missing required field"))
    pool = _pool_with(bad)

    async def fail_connect(name, cfg):  # must NOT be called
        raise AssertionError("reconnect attempted on a non-transport error")

    pool._connect = fail_connect  # type: ignore[assignment]

    with pytest.raises(ValueError, match="validation"):
        asyncio.run(pool.call_tool("srv", "query_memory", {}))
    assert bad.calls == 1  # no retry


def test_reraises_when_reconnect_still_fails():
    dead = _Session(fail_with=RuntimeError("connection reset by peer"))
    still_dead = _Session(fail_with=RuntimeError("connection reset by peer"))
    pool = _pool_with(dead)

    async def fake_connect(name, cfg):
        pool.sessions[name] = still_dead

    pool._connect = fake_connect  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="connection reset"):
        asyncio.run(pool.call_tool("srv", "query_memory", {}))
    assert dead.calls == 1
    assert still_dead.calls == 1
