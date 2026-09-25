"""SHA-122: MCP server secrets must not be reachable from sandbox code.

Proxies `<server>.*` share the namespace with user code. Under in-process
isolation `srv._session` / `srv._caller` reached the live client session and
the pool's server configs (auth headers, env tokens). Private attributes are
now refused by the AST validator, like dunders.
"""

import asyncio
import sys

import pytest
from mcp.types import Tool

from code_runner.executor import CodeExecutor

SECRET = "SECRET-TOKEN-XYZ"


class _Session:
    headers = {"Authorization": f"Bearer {SECRET}"}

    async def call_tool(self, name, kwargs):
        raise AssertionError("not called")


class _Pool:
    configs = {"srv": {"env": {"API_TOKEN": SECRET}, "headers": {"Cookie": SECRET}}}
    sessions = {"srv": _Session()}
    tools = {"srv": [Tool(name="query", inputSchema={"type": "object"})]}

    async def call_tool(self, server, tool, kwargs):
        raise AssertionError("not called")


ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]

PROBES = [
    "print(srv._session.headers)",
    "print(srv._caller.configs)",
    "print(srv._caller.sessions['srv'].headers)",
    "print(dir(srv))",
    "print(vars(srv))",
    "print(getattr(srv, '_caller'))",
    "print(globals())",
]


@pytest.mark.parametrize("isolation", ISOLATIONS)
@pytest.mark.parametrize("code", PROBES)
def test_secret_not_reachable(isolation, code):
    ex = CodeExecutor(_Pool(), isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(ex.execute(code, timeout=5))
    seen = f"{result.get('output')}{result.get('error')}"
    assert SECRET not in seen
    assert result["success"] is False, seen


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_private_attribute_refused_by_validator(isolation):
    ex = CodeExecutor(_Pool(), isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(ex.execute("x = srv._caller", timeout=5))
    assert "private attribute access is not allowed: _caller" in result["error"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_namedtuple_api_still_allowed(isolation):
    ex = CodeExecutor(_Pool(), isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(ex.execute(
        "P = collections.namedtuple('P', 'a b')\n"
        "p = P(1, 2)\n"
        "print(p._asdict(), p._replace(a=3), P._fields, P._make([4, 5]))",
        timeout=5,
    ))
    assert result["success"] is True, result["error"]


FORMAT_PROBES = [
    "print('{0._caller.configs}'.format(srv))",
    "print(string.Formatter().format('{0._session.headers}', srv))",
]


@pytest.mark.parametrize(
    "isolation",
    [
        pytest.param(
            "inprocess",
            marks=pytest.mark.xfail(
                strict=True,
                reason="AST filter is not a boundary: str.format resolves "
                "attributes at runtime; only subprocess isolates secrets",
            ),
        ),
        ISOLATIONS[1],
    ],
)
@pytest.mark.parametrize("code", FORMAT_PROBES)
def test_format_string_attribute_lookup(isolation, code):
    ex = CodeExecutor(_Pool(), isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(ex.execute(code, timeout=5))
    assert SECRET not in f"{result.get('output')}{result.get('error')}"
