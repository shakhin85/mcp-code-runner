"""MCP server secrets must not be reachable from sandbox code.

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
    # __match_args__ turns a positional class pattern into getattr by string
    "M = type('M', (type,), {'__instancecheck__': lambda c, o: True})\n"
    "X = M('X', (), {'__match_args__': ('_caller',)})\n"
    "match srv:\n    case X(c):\n        print(c.configs)",
    "M = dataclasses.make_dataclass('M', [], bases=(type,), init=False,"
    " namespace={'__instancecheck__': lambda c, o: True})\n"
    "X = M('X', (), {'__match_args__': ('_caller',)})\n"
    "match srv:\n    case X(c):\n        print(c.configs)",
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


@pytest.mark.parametrize(
    "code",
    [
        "match 1:\n    case int(_caller=c):\n        pass",
        "match 1:\n    case int(__class__=c):\n        pass",
        "match '':\n    case str(format=f):\n        pass",
        "match 1:\n    case int(x):\n        pass",
    ],
)
def test_class_pattern_attribute_refused(code):
    ex = CodeExecutor(_Pool(), isolation="inprocess", mem_limit_mb=1024)
    result = asyncio.run(ex.execute(code, timeout=5))
    assert "not allowed" in result["error"]


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


# SHA-487: a format string resolves `{0._x}` at runtime, past the AST rule.
FORMAT_PROBES = [
    "print('{0._caller.configs}'.format(srv))",
    "print('{s._caller.configs}'.format_map({'s': srv}))",
    "print(str.format('{0._caller.configs}', srv))",
    "f = '{0._caller.configs}'.format\nprint(f(srv))",
    "print(list(map(str.format, ['{0._caller.configs}'], [srv])))",
    "print(string.Formatter().format('{0._session.headers}', srv))",
    "print(string.Formatter().vformat('{0._session.headers}', (srv,), {}))",
    "print(__cr_format_attr__(srv, '_caller').configs)",
    "print(collections.UserString('{0._caller.configs}').format(srv))",
    "U = collections.UserString\nprint(U.format_map(U('{s._caller.configs}'), {'s': srv}))",
    "match '{0._caller.configs}':\n    case str(format=f):\n        print(f(srv))",
]


@pytest.mark.parametrize("isolation", ISOLATIONS)
@pytest.mark.parametrize("code", FORMAT_PROBES)
def test_format_string_attribute_lookup(isolation, code):
    ex = CodeExecutor(_Pool(), isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(ex.execute(code, timeout=5))
    assert SECRET not in f"{result.get('output')}{result.get('error')}"
    assert result["success"] is False


@pytest.mark.parametrize("isolation", ISOLATIONS)
@pytest.mark.parametrize("field", ["{0._caller}", "{0.__class__}"])
def test_format_private_field_refused(isolation, field):
    ex = CodeExecutor(_Pool(), isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(ex.execute(f"'{field}'.format(srv)", timeout=5))
    assert "attribute is not allowed in a format field" in result["error"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_format_public_fields_still_work(isolation):
    ex = CodeExecutor(_Pool(), isolation=isolation, mem_limit_mb=1024)
    result = asyncio.run(ex.execute(
        "print('{0} {a[x]} {b.real:>3} {0!r}'.format('p', a={'x': 2}, b=3),"
        " '{k}'.format_map({'k': 4}), str.format('{}', 5),"
        " string.Formatter().format('{0.imag}', 6), '{}'.format(7),"
        " collections.UserString('{}').format(8))",
        timeout=5,
    ))
    assert result["success"] is True, result["error"]
    assert result["output"].strip() == "p 2   3 'p' 4 5 0 7 8"
