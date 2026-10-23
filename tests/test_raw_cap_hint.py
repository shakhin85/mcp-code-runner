"""RAW-CAP hint names the call, the true response size and raw_cap (SHA-486)."""

import asyncio

import pytest

from code_runner.executor import CodeExecutor
from code_runner.workspace import WorkspaceManager

ISOLATIONS = ["inprocess", "subprocess"]
PAYLOAD = '[{"a": "' + "x" * 5000 + '"}]'
RAW_CAP = 1000


class _Text:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, text):
        self.content = [_Text(text)]
        self.isError = False


class _Session:
    async def call_tool(self, name, kwargs):
        return _Result(PAYLOAD)


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = ""


class _Pool:
    sessions = {"pg": _Session()}
    tools = {"pg": [_Tool("execute_sql")]}


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_capped_result_indexing_names_call_size_and_cap(tmp_path, isolation):
    ex = CodeExecutor(_Pool(), workspace=WorkspaceManager(tmp_path), isolation=isolation)
    r = asyncio.run(ex.execute(
        "rows = await pg.execute_sql(sql='select a from t limit 1')\nprint(rows[0]['a'])",
        raw_cap=RAW_CAP,
    ))
    assert r["success"] is False
    size = len(PAYLOAD.encode("utf-8"))
    for needle in ("pg.execute_sql", f"returned {size} B", f"raw_cap={RAW_CAP}"):
        assert needle in r["error"], r["error"]


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_receipt_warning_names_capped_call(tmp_path, isolation):
    ex = CodeExecutor(_Pool(), workspace=WorkspaceManager(tmp_path), isolation=isolation)
    r = asyncio.run(ex.execute(
        "rows = await pg.execute_sql(sql='select a from t limit 1')\nprint(type(rows).__name__)",
        raw_cap=RAW_CAP,
    ))
    assert r["success"] is True, r["error"]
    assert "hit RAW-CAP" in r["output"]
    assert f"pg.execute_sql returned {len(PAYLOAD.encode('utf-8'))} B" in r["output"]
    assert f"raw_cap={RAW_CAP}" in r["output"]
