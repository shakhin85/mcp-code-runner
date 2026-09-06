"""Regression: a client that never answers roots/list must not hang the call.

`_resolve_project_dir` runs on the FIRST execute_code of every MCP session and
issues a server->client `roots/list`. Without a timeout a raw client that does
not implement the callback left the call pending forever (not slow — infinite):
restarting the backend and skipping servers changed nothing, because execution
never reached the pools.
"""

import asyncio

import pytest

from code_runner import server as server_mod


class _Session:
    """Client session whose roots/list never answers."""

    async def list_roots(self):
        await asyncio.Event().wait()


class _Ctx:
    def __init__(self, session):
        self.session = session


def test_silent_client_falls_back_to_global_pool(monkeypatch, caplog):
    monkeypatch.setattr(server_mod, "ROOTS_TIMEOUT", 0.05)
    ctx = _Ctx(_Session())

    async def scenario():
        return await asyncio.wait_for(server_mod._resolve_project_dir(ctx), timeout=5)

    assert asyncio.run(scenario()) is None
    # The verdict is cached, so the next call pays nothing.
    assert ctx.session in server_mod._session_roots
    assert any("timed out" in r.message for r in caplog.records)


def test_roots_result_still_resolves_project_dir(monkeypatch, tmp_path):
    class _Root:
        uri = tmp_path.as_uri()

    class _Result:
        roots = [_Root()]

    class _Ok:
        async def list_roots(self):
            return _Result()

    ctx = _Ctx(_Ok())
    assert asyncio.run(server_mod._resolve_project_dir(ctx)) == tmp_path


def test_debug_roots_reports_timeout_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(server_mod, "ROOTS_TIMEOUT", 0.05)
    ctx = _Ctx(_Session())
    fn = getattr(server_mod.debug_roots, "fn", server_mod.debug_roots)

    async def scenario():
        return await asyncio.wait_for(fn(ctx), timeout=5)

    assert "TimeoutError" in asyncio.run(scenario())


if __name__ == "__main__":
    pytest.main([__file__])
