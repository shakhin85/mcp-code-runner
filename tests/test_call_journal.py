"""SHA-124: per-session journal of downstream tool calls.

Debugging a hang (SHA-129 A.4) had only the aggregates in metrics.jsonl, not
the calls of one concrete run. Each proxied call now appends one JSON line
(server, tool, args, duration, bytes, success, error) to `calls.jsonl` in the
session workspace, readable from sandbox code with `open('calls.jsonl')`.
"""

import asyncio
import json
import sys

import pytest
from mcp.types import Tool

from code_runner.executor import CodeExecutor
from code_runner.workspace import WorkspaceManager


class _Content:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, text, is_error=False):
        self.content = [_Content(text)]
        self.isError = is_error


class _Session:
    async def call_tool(self, name, kwargs):
        if name == "boom":
            return _Result("downstream failed", is_error=True)
        return _Result("hello")


class _Pool:
    sessions = {"srv": _Session()}
    tools = {
        "srv": [
            Tool(name="query", inputSchema={"type": "object"}),
            Tool(name="boom", inputSchema={"type": "object"}),
            Tool(name="delete_row", inputSchema={"type": "object"}),
        ]
    }


ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]


def _journal(tmp_path, sid):
    path = tmp_path / sid / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_calls_are_journaled_per_session(tmp_path, isolation):
    ex = CodeExecutor(
        _Pool(), workspace=WorkspaceManager(tmp_path), isolation=isolation,
        mem_limit_mb=1024,
    )
    code = (
        "await srv.query(sql='select 1')\n"
        "try:\n"
        "    await srv.boom(x=1)\n"
        "except Exception:\n"
        "    pass\n"
    )
    result = asyncio.run(ex.execute(code, timeout=5, session_id="s1"))
    assert result["success"] is True, result["error"]

    rows = _journal(tmp_path, "s1")
    assert [(r["server"], r["tool"], r["success"]) for r in rows] == [
        ("srv", "query", True),
        ("srv", "boom", False),
    ]
    assert rows[0]["args"] == {"sql": "select 1"}
    assert rows[0]["bytes"] == len("hello")
    assert rows[0]["error"] is None
    assert "downstream failed" in rows[1]["error"]
    assert all(isinstance(r["duration_ms"], float) for r in rows)
    assert all("ts" in r for r in rows)


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_journal_readable_from_sandbox(tmp_path, isolation):
    ex = CodeExecutor(
        _Pool(), workspace=WorkspaceManager(tmp_path), isolation=isolation,
        mem_limit_mb=1024,
    )
    asyncio.run(ex.execute("await srv.query(q=1)", timeout=5, session_id="s2"))
    result = asyncio.run(ex.execute(
        "print(len(open('calls.jsonl').read().splitlines()))",
        timeout=5, session_id="s2",
    ))
    assert result["success"] is True, result["error"]
    assert result["output"].strip() == "1"


def test_long_args_truncated(tmp_path):
    ex = CodeExecutor(_Pool(), workspace=WorkspaceManager(tmp_path),
                      isolation="inprocess")
    asyncio.run(ex.execute(
        "await srv.query(sql='x' * 5000)", timeout=5, session_id="s3"
    ))
    [row] = _journal(tmp_path, "s3")
    assert isinstance(row["args"], str)
    assert len(row["args"]) <= 520
    assert row["args"].endswith("…[truncated]")


def test_no_session_no_journal(tmp_path):
    ex = CodeExecutor(_Pool(), workspace=WorkspaceManager(tmp_path),
                      isolation="inprocess")
    result = asyncio.run(ex.execute("await srv.query(q=1)", timeout=5))
    assert result["success"] is True, result["error"]
    assert list(tmp_path.iterdir()) == []


def test_denied_side_effect_not_journaled(tmp_path):
    ex = CodeExecutor(_Pool(), workspace=WorkspaceManager(tmp_path),
                      isolation="inprocess")
    asyncio.run(ex.execute(
        "await srv.query(q=1)\nawait srv.delete_row(id=1)",
        timeout=5, session_id="s4",
    ))
    assert [r["tool"] for r in _journal(tmp_path, "s4")] == ["query"]


def test_session_id_without_workspace_still_runs(tmp_path):
    """An id the workspace rejects (`:` is invalid) still gets session vars;
    it just has no workspace, so no journal — same as open() refusing lazily."""
    ex = CodeExecutor(_Pool(), workspace=WorkspaceManager(tmp_path),
                      isolation="inprocess")
    result = asyncio.run(ex.execute(
        "await srv.query(q=1)\nprint('ok')", timeout=5, session_id="bad:id"
    ))
    assert result["success"] is True, result["error"]
    assert list(tmp_path.iterdir()) == []
