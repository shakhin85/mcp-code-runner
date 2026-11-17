"""A positional argument to an MCP proxy names the tool and its keyword parameters.

Proxies take keyword arguments only (they become the tool's JSON arguments).
``execute_sql("select 1")`` used to die with
``call() takes 0 positional arguments but 1 was given``: no tool, no parameter
names, nothing to fix it from. Both isolations must say the same thing.
"""

import asyncio

import pytest

from code_runner.executor import CodeExecutor

ISOLATIONS = ["inprocess", "subprocess"]


class _Tool:
    def __init__(self, name, params):
        self.name = name
        self.description = ""
        self.inputSchema = {"type": "object", "properties": {p: {"type": "string"} for p in params}}


class _Pool:
    sessions = {"postgres-lime-prod": object()}
    tools = {"postgres-lime-prod": [_Tool("execute_sql", ["sql"])]}


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_positional_arg_names_keyword_form(isolation):
    ex = CodeExecutor(_Pool(), isolation=isolation)
    r = asyncio.run(ex.execute("await postgres_lime_prod.execute_sql('select 1')"))
    assert r["success"] is False
    assert (
        "postgres_lime_prod.execute_sql() принимает только keyword-аргументы: "
        "postgres_lime_prod.execute_sql(sql=...)"
    ) in r["error"], r["error"]
