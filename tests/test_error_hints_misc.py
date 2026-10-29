"""Holes 4-8 from the transcript scan: errors that cost the caller a blind retry.

Each test runs the sandbox end to end (both isolations where the hint is built
child-side) and asserts on what the caller sees: the error text or the receipt.
"""

import asyncio
import sys

import pytest

from code_runner.executor import CodeExecutor
from code_runner.skills import SkillLoader, SkillsNamespace

ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]


class _Text:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, text):
        self.content = [_Text(text)]
        self.isError = False


class _Session:
    def __init__(self, payload):
        self._payload = payload

    async def call_tool(self, name, kwargs):
        return _Result(self._payload)


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = ""


def _pool(payload="[]"):
    class Pool:
        sessions = {"exa": _Session(payload), "postgres": _Session(payload)}
        tools = {"exa": [_Tool("web_fetch_exa")], "postgres": [_Tool("execute_sql")]}
    return Pool()


def _skills(tmp_path):
    d = tmp_path / "skills" / "calc"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("calc\n", encoding="utf-8")
    (d / "script.py").write_text("def ok(x):\n    return {'v': x + 1}\n", encoding="utf-8")
    return SkillsNamespace(SkillLoader(tmp_path / "skills").discover())


def _run(code, isolation, pool=None, skills=None):
    ex = CodeExecutor(pool or _pool(), skills=skills, isolation=isolation, mem_limit_mb=1024)
    return asyncio.run(ex.execute(code, timeout=10))


# --- hole 4: SQL error delivered as plain text, isError unset ---------------


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_error_text_result_is_named_in_receipt(isolation):
    pool = _pool("Error: function foo(integer) does not exist")
    r = _run("r = await postgres.execute_sql(sql='select foo(1)')\nprint(len(r))", isolation, pool)
    assert r["success"] is True, r["error"]
    assert "returned error text without isError" in r["output"]
    assert "postgres.execute_sql" in r["output"]


def test_ordinary_text_result_has_no_error_warning():
    pool = _pool("Errors are rare here; 3 rows updated")
    r = _run("r = await postgres.execute_sql(sql='update t set a=1')\nprint(r)", "inprocess", pool)
    assert r["success"] is True, r["error"]
    assert "error text" not in r["output"]


# --- hole 5: from skills import X ------------------------------------------


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_from_skills_import_binds_the_skill(tmp_path, isolation):
    r = _run("from skills import calc\nprint(calc.ok(1)['v'])", isolation, skills=_skills(tmp_path))
    assert r["success"] is True, r["error"]
    assert "2" in r["output"]


def test_dotted_skills_import_stays_blocked(tmp_path):
    r = _run("from skills.calc import ok", "inprocess", skills=_skills(tmp_path))
    assert r["success"] is False


# --- hole 6: await on a sync skill, hasattr ----------------------------------


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_await_sync_skill_hint(tmp_path, isolation):
    r = _run("x = await skills.calc.ok(1)", isolation, skills=_skills(tmp_path))
    assert r["success"] is False
    assert "Убери await" in r["error"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_hasattr_hint(isolation):
    r = _run("print(hasattr({}, 'a'))", isolation)
    assert r["success"] is False
    assert "hasattr() в песочнице недоступен" in r["error"]
    assert "d.get('k')" in r["error"]


# --- hole 7: json.loads on an already-parsed proxy result --------------------


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_json_loads_on_parsed_result_hint(isolation):
    pool = _pool('[{"a": 1}]')
    r = _run("r = await postgres.execute_sql(sql='select 1')\njson.loads(r)", isolation, pool)
    assert r["success"] is False
    assert "уже вернул разобранный list" in r["error"]


# --- hole 8: Claude Code tool name used as a sandbox name --------------------


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_claude_code_tool_name_hint(isolation):
    r = _run("await mcp__exa__web_fetch_exa(url='x')", isolation)
    assert r["success"] is False
    assert "В песочнице это exa.web_fetch_exa(...)" in r["error"]


def test_claude_code_name_of_unknown_server_falls_back_to_roster():
    r = _run("await mcp__nope__tool()", "inprocess")
    assert r["success"] is False
    assert "доступные MCP-прокси: exa, postgres" in r["error"]
