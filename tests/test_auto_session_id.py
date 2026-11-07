"""execute_code without session_id gets a stable per-MCP-session `auto-<hash>` id."""

import asyncio
import re

import pytest

from code_runner import server as server_mod
from code_runner.executor import CodeExecutor
from code_runner.metrics import caller_of
from code_runner.workspace import _SESSION_ID_RE, WorkspaceError, WorkspaceManager


class _Session:
    pass


class _Ctx:
    def __init__(self):
        self.session = _Session()


def test_same_session_same_id_different_sessions_differ():
    a, b = _Ctx(), _Ctx()
    first = server_mod._effective_session_id(a, None)
    assert server_mod._effective_session_id(a, None) == first
    assert server_mod._effective_session_id(b, None) != first
    assert re.fullmatch(_SESSION_ID_RE.pattern, first)
    assert caller_of(first) == "auto"


def test_explicit_session_id_wins():
    assert server_mod._effective_session_id(_Ctx(), "main-x") == "main-x"


class _Pool:
    sessions: dict = {}
    tools: dict = {}


def _run(tmp_path, code, sid, persist):
    ex = _EX[tmp_path]
    return asyncio.run(ex.execute(code, session_id=sid, persist_vars=persist))


_EX: dict = {}


def _executor(tmp_path):
    ex = CodeExecutor(_Pool(), workspace=WorkspaceManager(tmp_path), isolation="inprocess")
    _EX[tmp_path] = ex
    return ex


def test_derived_id_shares_workspace_but_not_variables(tmp_path):
    _executor(tmp_path)
    code = "rows = 1\nwith open('a.txt', 'w') as f:\n    f.write('x')"
    r1 = _run(tmp_path, code, "auto-1", False)
    assert r1["success"], r1["error"]
    r2 = _run(tmp_path, "print(rows)", "auto-1", False)
    assert not r2["success"] and "NameError" in r2["error"]
    r3 = _run(tmp_path, "print(open('a.txt').read())", "auto-1", False)
    assert r3["success"] and "x" in r3["output"]


def test_explicit_id_persists_variables(tmp_path):
    _executor(tmp_path)
    assert _run(tmp_path, "rows = 7", "main-x", True)["success"]
    r = _run(tmp_path, "print(rows)", "main-x", True)
    assert r["success"] and "7" in r["output"]


def test_explicit_auto_prefix_refused():
    with pytest.raises(WorkspaceError, match="зарезервирован"):
        server_mod._effective_session_id(_Ctx(), "auto-guessed")


def test_auto_id_is_full_uuid_and_fits_validator():
    sid = server_mod._effective_session_id(_Ctx(), None)
    assert len(sid) == len("auto-") + 32 <= 64
