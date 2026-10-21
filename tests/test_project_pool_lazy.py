"""SHA-456: a run on global servers must not wait for the project pool startup.

Before the fix, every execute_code awaited ProjectPoolRegistry startup of ALL
project servers (up to STARTUP_TIMEOUT=60s), even when the code touched only
global servers.
"""

import asyncio
from types import SimpleNamespace

from code_runner import project_pools
from code_runner import server as server_mod
from code_runner.project_pools import ProjectPoolRegistry


def _ctx(registry):
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"project_pools": registry})
    )


def _setup(monkeypatch, tmp_path, release: asyncio.Event):
    async def slow_startup(self, skip_servers=None, configs=None):
        await release.wait()

    monkeypatch.setattr(project_pools.MCPClientPool, "startup", slow_startup)
    monkeypatch.setattr(
        project_pools,
        "load_project_server_configs",
        lambda project_dir, skip_servers, exclude: {"slow-srv": object()},
    )

    async def resolve(ctx):
        return tmp_path

    monkeypatch.setattr(server_mod, "_resolve_project_dir", resolve)
    return ProjectPoolRegistry(global_names={"postgres-lime"}, skip_servers=set())


def test_global_only_code_does_not_wait(monkeypatch, tmp_path):
    async def scenario():
        release = asyncio.Event()
        registry = _setup(monkeypatch, tmp_path, release)
        pool = await asyncio.wait_for(
            server_mod._project_pool(_ctx(registry), "await postgres_lime.q(sql='1')"),
            timeout=1,
        )
        assert pool is None
        release.set()
        await registry.shutdown()

    asyncio.run(scenario())


def test_project_server_code_waits_for_its_pool(monkeypatch, tmp_path):
    async def scenario():
        release = asyncio.Event()
        registry = _setup(monkeypatch, tmp_path, release)
        task = asyncio.create_task(
            server_mod._project_pool(_ctx(registry), "await slow_srv.t()")
        )
        await asyncio.sleep(0.05)
        assert not task.done()
        release.set()
        pool = await asyncio.wait_for(task, timeout=1)
        assert pool is not None
        await registry.shutdown()

    asyncio.run(scenario())


def test_unparsable_code_waits(monkeypatch, tmp_path):
    async def scenario():
        release = asyncio.Event()
        registry = _setup(monkeypatch, tmp_path, release)
        release.set()
        pool = await server_mod._project_pool(_ctx(registry), "def (")
        assert pool is not None
        await registry.shutdown()

    asyncio.run(scenario())
