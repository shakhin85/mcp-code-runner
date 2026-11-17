"""SHA-994: a call waits only for the servers it uses, not for the slowest one.

Наблюдение 17.11.2026: первый execute_sql после рестарта backend шёл 30 s —
глобальный пул ждал CONNECTION_TIMEOUT мёртвых eraser и bp-1c-svod, хотя
postgres_lime_prod подключился за 6 s.
"""

import asyncio
from types import SimpleNamespace

import pytest

from code_runner import server as server_mod
from code_runner.client_pool import MCPClientPool


def _fake_connect(release: asyncio.Event):
    async def connect(self, name, cfg):
        if name == "slow":
            await release.wait()
        session = SimpleNamespace(call_tool=_echo(name))
        self.sessions[name] = session
        self.tools[name] = []

    return connect


def _echo(name):
    async def call_tool(tool, args):
        return f"{name}.{tool}"

    return call_tool


def _configs():
    return {"fast": object(), "slow": object()}


def test_call_to_fast_server_does_not_wait_for_slow(monkeypatch):
    async def scenario():
        release = asyncio.Event()
        monkeypatch.setattr(MCPClientPool, "_connect", _fake_connect(release))
        pool = MCPClientPool()
        pool.begin(configs=_configs())
        await asyncio.wait_for(pool.wait_ready({"fast"}), timeout=1)
        assert await asyncio.wait_for(pool.call_tool("fast", "q", {}), timeout=1) == "fast.q"
        assert "slow" not in pool.sessions
        release.set()
        await pool.wait_ready()
        assert set(pool.sessions) == {"fast", "slow"}
        await pool.shutdown()

    asyncio.run(scenario())


def test_call_to_pending_server_waits_for_it(monkeypatch):
    async def scenario():
        release = asyncio.Event()
        monkeypatch.setattr(MCPClientPool, "_connect", _fake_connect(release))
        pool = MCPClientPool()
        pool.begin(configs=_configs())
        call = asyncio.create_task(pool.call_tool("slow", "q", {}))
        await asyncio.sleep(0.05)
        assert not call.done()
        release.set()
        assert await asyncio.wait_for(call, timeout=1) == "slow.q"
        await pool.shutdown()

    asyncio.run(scenario())


def test_failed_server_still_raises_not_connected(monkeypatch):
    async def connect(self, name, cfg):
        raise TimeoutError("dead")

    async def scenario():
        monkeypatch.setattr(MCPClientPool, "_connect", connect)
        pool = MCPClientPool()
        pool.begin(configs={"dead": object()})
        with pytest.raises(RuntimeError, match="not connected"):
            await pool.call_tool("dead", "q", {})
        assert "dead" in pool.failed

    asyncio.run(scenario())


def test_shutdown_cancels_pending_connects(monkeypatch):
    async def scenario():
        monkeypatch.setattr(MCPClientPool, "_connect", _fake_connect(asyncio.Event()))
        pool = MCPClientPool()
        pool.begin(configs=_configs())
        await pool.wait_ready({"fast"})
        await asyncio.wait_for(pool.shutdown(), timeout=1)

    asyncio.run(scenario())


NAMES = frozenset({"postgres_lime_prod", "bp-1c-svod", "eraser"})


@pytest.mark.parametrize(
    ("code", "needed"),
    [
        ('await postgres_lime_prod.execute_sql(sql="select 1")', {"postgres_lime_prod"}),
        ("await bp_1c_svod.q()", {"bp-1c-svod"}),
        ("print(1)", set()),
        ("print(skills.x.f())", None),
        ("fg_query('x')", None),
        ("def (:", None),
    ],
)
def test_servers_needed(code, needed):
    assert server_mod._servers_needed(code, NAMES) == needed
