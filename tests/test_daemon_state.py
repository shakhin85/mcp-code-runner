"""Regression: daemon-mode lifespan state is a process-wide singleton.

In streamable-http mode FastMCP enters `lifespan` once per MCP *session*,
not once per process. Building pools there spawned a full copy of every
global MCP server per session until TasksMax exhaustion; leaving a session
must also NOT tear the shared state down.
"""

import asyncio

from code_runner import server as server_mod


def test_daemon_lifespan_shares_one_state(monkeypatch):
    monkeypatch.setenv("CODE_RUNNER_TRANSPORT", "streamable-http")
    monkeypatch.setattr(server_mod, "load_server_configs", lambda skip: {})
    monkeypatch.setattr(server_mod, "_daemon_state", None)
    monkeypatch.setattr(server_mod, "_daemon_lock", asyncio.Lock())

    async def scenario():
        async with server_mod.lifespan(server_mod.mcp) as first:
            pass
        # A second session must reuse the same state object — and the first
        # session's exit must not have torn the shared pool down.
        async with server_mod.lifespan(server_mod.mcp) as second:
            assert second is first
        assert first["project_pools"] is not None
        await first["_pool_host"].stop()

    asyncio.run(scenario())


def test_stdio_lifespan_owns_state_per_process(monkeypatch):
    monkeypatch.delenv("CODE_RUNNER_TRANSPORT", raising=False)
    monkeypatch.setattr(
        server_mod.MCPClientPool,
        "startup",
        lambda self, skip_servers=None, configs=None: _noop(),
    )
    monkeypatch.setattr(server_mod.MCPClientPool, "shutdown", lambda self: _noop())

    async def scenario():
        async with server_mod.lifespan(server_mod.mcp) as first:
            pass
        async with server_mod.lifespan(server_mod.mcp) as second:
            assert second is not first
        assert first["project_pools"] is None

    asyncio.run(scenario())


async def _noop():
    return None
