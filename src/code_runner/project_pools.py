"""
Per-project downstream pools for daemon (streamable-http) mode.

Один общий демон обслуживает сессии разных проектов; серверы из проектного
`.mcp.json` поднимаются лениво по первому запросу сессии этого проекта и
живут в отдельном MCPClientPool на проект (не глобально — иначе коллизии
имён между проектами). Простаивающие пулы гасятся по TTL.

Каждый пул живёт внутри собственного asyncio.Task (_PoolHost): anyio
cancel-scopes stdio-клиентов привязаны к задаче, поэтому startup и shutdown
обязаны выполняться в одной и той же задаче — иначе RuntimeError при
закрытии из чужого request-таска.
"""

import asyncio
import logging
import time
from pathlib import Path

from .client_pool import MCPClientPool
from .config_reader import load_project_server_configs

logger = logging.getLogger(__name__)

IDLE_TTL_SECONDS = 30 * 60
STARTUP_TIMEOUT = 60.0


class _PoolHost:
    """Owns one pool's lifecycle inside a dedicated task."""

    def __init__(self, project_dir: Path, configs: dict):
        self.pool = MCPClientPool()
        self.project_dir = project_dir
        self._stop = asyncio.Event()
        self.ready = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(configs), name=f"project-pool:{project_dir}"
        )

    async def _run(self, configs: dict) -> None:
        try:
            await self.pool.startup(configs=configs)
        except Exception as e:
            logger.warning(f"Project pool startup failed for {self.project_dir}: {e}")
        finally:
            self.ready.set()
        await self._stop.wait()
        try:
            await self.pool.shutdown()
        except Exception as e:
            logger.warning(f"Project pool shutdown error for {self.project_dir}: {e}")

    async def stop(self) -> None:
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=15)
        except Exception as e:
            logger.warning(f"Project pool stop error for {self.project_dir}: {e}")


class ProjectPoolRegistry:
    """project_dir -> lazily started MCPClientPool with idle eviction."""

    def __init__(self, global_names: set[str], skip_servers: set[str]):
        self._global_names = global_names
        self._skip = skip_servers
        self._hosts: dict[Path, _PoolHost] = {}
        self._last_access: dict[Path, float] = {}
        self._lock = asyncio.Lock()

    async def get(self, project_dir: Path) -> MCPClientPool | None:
        """Pool for this project, or None if it adds no servers beyond global."""
        project_dir = project_dir.resolve()
        async with self._lock:
            await self._evict_idle_locked()
            host = self._hosts.get(project_dir)
            if host is None:
                configs = load_project_server_configs(
                    project_dir,
                    skip_servers=self._skip,
                    exclude=self._global_names,
                )
                if not configs:
                    return None
                logger.info(
                    f"Starting project pool for {project_dir}: {sorted(configs)}"
                )
                host = _PoolHost(project_dir, configs)
                self._hosts[project_dir] = host
            self._last_access[project_dir] = time.monotonic()
        await asyncio.wait_for(host.ready.wait(), timeout=STARTUP_TIMEOUT)
        return host.pool

    async def _evict_idle_locked(self) -> None:
        now = time.monotonic()
        expired = [
            d for d, t in self._last_access.items()
            if now - t > IDLE_TTL_SECONDS and d in self._hosts
        ]
        for d in expired:
            host = self._hosts.pop(d)
            self._last_access.pop(d, None)
            logger.info(f"Evicting idle project pool: {d}")
            await host.stop()

    async def shutdown(self) -> None:
        async with self._lock:
            hosts = list(self._hosts.values())
            self._hosts.clear()
            self._last_access.clear()
        for host in hosts:
            await host.stop()
