"""session_id: ':' is accepted, a bad id is refused before any downstream call."""

import asyncio

import pytest

from code_runner.executor import CodeExecutor
from code_runner.workspace import WorkspaceManager

ISOLATIONS = ["inprocess", "subprocess"]


class _Text:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, text):
        self.content = [_Text(text)]
        self.isError = False


class _Session:
    def __init__(self):
        self.calls = 0

    async def call_tool(self, name, kwargs):
        self.calls += 1
        return _Result("ok")


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = ""


class _Pool:
    def __init__(self):
        self.sessions = {"srv": _Session()}
        self.tools = {"srv": [_Tool("ping")]}


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_colon_session_id_writes_to_workspace(tmp_path, isolation):
    ex = CodeExecutor(_Pool(), workspace=WorkspaceManager(tmp_path), isolation=isolation)
    r = asyncio.run(ex.execute(
        "with open('a.txt', 'w') as f:\n    f.write('x')",
        session_id="web-research:roles",
    ))
    assert r["success"] is True, r["error"]
    assert (tmp_path / "web-research:roles" / "a.txt").read_text() == "x"


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_bad_session_id_refused_before_tool_call(tmp_path, isolation):
    pool = _Pool()
    ex = CodeExecutor(pool, workspace=WorkspaceManager(tmp_path), isolation=isolation)
    r = asyncio.run(ex.execute("await srv.ping()", session_id="a/b"))
    assert r["success"] is False
    assert "invalid session_id" in r["error"]
    assert pool.sessions["srv"].calls == 0
