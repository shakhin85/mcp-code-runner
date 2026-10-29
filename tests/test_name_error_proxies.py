"""NameError HINT lists the MCP proxies under both isolations.

Under subprocess the child's proxies are ``_RemoteProxy``, not ``_ToolNamespace``;
the roster used to come out empty there, so a guessed server name stayed a guess.
"""

import asyncio

import pytest

from code_runner.executor import CodeExecutor

ISOLATIONS = ["inprocess", "subprocess"]


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = ""


class _Pool:
    sessions = {"postgres-lime-prod": object(), "zup-1c": object()}
    tools = {"postgres-lime-prod": [_Tool("execute_sql")], "zup-1c": [_Tool("query")]}


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_unknown_server_name_lists_proxies(isolation):
    ex = CodeExecutor(_Pool(), isolation=isolation)
    r = asyncio.run(ex.execute("await postgres_lime.execute_sql(sql='select 1')"))
    assert r["success"] is False
    assert "доступные MCP-прокси: postgres_lime_prod, zup_1c" in r["error"], r["error"]
