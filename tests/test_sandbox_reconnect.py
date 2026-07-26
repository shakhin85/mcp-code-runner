"""Regression: the sandbox tool-call path (`await <server>.<tool>(...)` inside
execute_code) must reconnect on a dead MCP session too.

`_ToolNamespace` used to capture `session` once and call
`session.call_tool()` directly, bypassing `MCPClientPool.call_tool()` and its
reconnect logic entirely. That meant every `execute_code` call kept handing
out the same stale session forever after an upstream server restart (e.g.
forgetful-backend), even though the pool-level reconnect worked fine when
called directly. This test drives the real sandbox path — CodeExecutor.execute
running user code that calls a proxied MCP tool via the `_ToolNamespace`
wrapper — not `pool.call_tool()` directly.
"""

import asyncio

from code_runner.client_pool import MCPClientPool
from code_runner.config_reader import ServerConfig
from code_runner.executor import CodeExecutor


class _FakeText:
    def __init__(self, text):
        self.text = text


class _FakeResult:
    def __init__(self, text):
        self.content = [_FakeText(text)]
        self.isError = False


class _FakeTool:
    def __init__(self, name):
        self.name = name
        self.description = ""


class _Session:
    def __init__(self, *, fail_with=None, payload="ok"):
        self.fail_with = fail_with
        self.payload = payload
        self.calls = 0

    async def call_tool(self, name, kwargs):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return _FakeResult(self.payload)


def test_sandbox_execute_code_reconnects_through_tool_namespace():
    dead = _Session(fail_with=RuntimeError("McpError: Session terminated"))
    fresh = _Session(payload='"recovered"')

    pool = MCPClientPool()
    pool.configs["forgetful"] = ServerConfig(
        name="forgetful", transport="http", url="http://127.0.0.1:8020/mcp"
    )
    pool.sessions["forgetful"] = dead
    pool.tools["forgetful"] = [_FakeTool("discover_forgetful_tools")]

    async def fake_connect(name, cfg):
        pool.sessions[name] = fresh

    pool._connect = fake_connect  # type: ignore[assignment]

    executor = CodeExecutor(pool)
    code = 'result = await forgetful.discover_forgetful_tools()'

    result = asyncio.run(executor.execute(code))

    assert result["error"] is None, result["error"]
    assert result["success"] is True
    assert dead.calls == 1  # sandbox call hit the dead session once
    assert fresh.calls == 1  # ... then the reconnected session served it


def test_namespace_resolves_caller_once_at_build_time():
    """Гармонизация двух фиксов: выбор «пул или сессия» делается один раз при
    сборке namespace, а не в каждом вызове инструмента.

    Пул с call_tool → зовём пул (реконнект работает). Дубль пула без call_tool
    (такие есть в юнит-тестах) → адаптер над сессией, поведение прежнее."""
    from code_runner.executor import _DirectSessionCaller, _ToolNamespace

    class _Pool:
        async def call_tool(self, server_name, tool_name, arguments):
            return "via-pool"

    class _Stub:  # дубль, стабящий только sessions/tools
        sessions: dict = {}
        tools: dict = {}

    real = _ToolNamespace("srv", session=object(), tools=[], pool=_Pool())
    assert not isinstance(real._caller, _DirectSessionCaller)

    stubbed = _ToolNamespace("srv", session=object(), tools=[], pool=_Stub())
    assert isinstance(stubbed._caller, _DirectSessionCaller)

    no_pool = _ToolNamespace("srv", session=object(), tools=[])
    assert isinstance(no_pool._caller, _DirectSessionCaller)
