"""Receipt names plausible-but-wrong results; truncated output is kept in the workspace."""

import asyncio

from code_runner.executor import CodeExecutor, _append_cost_footer, _result_shape
from code_runner.workspace import WorkspaceManager


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


def _executor(tmp_path, payload):
    class FakePool:
        sessions = {"postgres": _Session(payload)}
        tools = {"postgres": [_Tool("execute_sql")]}
    return CodeExecutor(FakePool(), workspace=WorkspaceManager(tmp_path))


# --- shape classifier ---


def test_shape_empty_list():
    assert _result_shape([], False, 0) == "empty_results"


def test_shape_saturated_limit():
    assert _result_shape([{"a": 1}] * 3, True, 3) == "limit_saturated"


def test_shape_not_saturated_without_injected_limit():
    assert _result_shape([{"a": 1}] * 3, False, 3) is None


def test_shape_all_none_rows():
    assert _result_shape([{"a": None, "b": None}], False, 0) == "all_none_results"


def test_shape_normal_rows_and_dicts_pass():
    assert _result_shape([{"a": 1, "b": None}], False, 0) is None
    assert _result_shape({"rows": []}, False, 0) is None


# --- receipt ---


def test_receipt_warns_on_quiet_failures():
    stats = {"tool_calls": 3, "raw_tool_bytes": 10, "empty_results": 2, "raw_capped": 1}
    out = _append_cost_footer("x", stats, elapsed_ms=1)
    assert "⚠" in out
    assert "2 calls returned 0 rows" in out
    assert "1 call hit RAW-CAP" in out


def test_receipt_silent_on_clean_run():
    out = _append_cost_footer("x", {"tool_calls": 2, "raw_tool_bytes": 10}, elapsed_ms=1)
    assert "⚠" not in out


def test_empty_sql_result_reaches_receipt(tmp_path):
    ex = _executor(tmp_path, "[]")
    r = asyncio.run(ex.execute(
        "rows = await postgres.execute_sql(sql='select 1')\nprint(len(rows))"
    ))
    assert r["success"] is True, r["error"]
    assert "1 call returned 0 rows" in r["output"]


def test_saturated_auto_limit_reaches_receipt(tmp_path):
    ex = _executor(tmp_path, '[{"a": 1}, {"a": 2}]')
    r = asyncio.run(ex.execute(
        "rows = await postgres.execute_sql(sql='select a from t')\nprint(len(rows))",
        auto_limit=2,
    ))
    assert r["success"] is True, r["error"]
    assert "hit the auto-LIMIT" in r["output"]


# --- reversible truncation ---


def test_truncated_output_saved_to_workspace(tmp_path):
    ex = _executor(tmp_path, "[]")
    r = asyncio.run(ex.execute("print('x' * 5000 + 'TAIL')", max_output_bytes=100, session_id="t1"))
    assert "restore(1, " in r["output"]
    saved = (tmp_path / "t1" / "out_1.txt").read_text(encoding="utf-8")
    assert saved.rstrip().endswith("TAIL")
    r2 = asyncio.run(ex.execute("print(open('out_1.txt').read()[-5:])", session_id="t1"))
    assert "TAIL" in r2["output"]


class _CountingSession(_Session):
    calls = 0

    async def call_tool(self, name, kwargs):
        type(self).calls += 1
        return await super().call_tool(name, kwargs)


def test_restore_returns_fragment_without_downstream_call(tmp_path):
    rows = ",".join(f'{{"id": {i}, "name": "row{i}"}}' for i in range(300))
    session = _CountingSession(f"[{rows}]")

    class FakePool:
        sessions = {"postgres": session}
        tools = {"postgres": [_Tool("execute_sql")]}

    ex = CodeExecutor(FakePool(), workspace=WorkspaceManager(tmp_path))
    r = asyncio.run(ex.execute(
        "rows = await postgres.execute_sql(sql='select id, name from t limit 300')\n"
        "for x in rows: print(x['id'], x['name'])",
        max_output_bytes=200, session_id="t1",
    ))
    assert r["success"] is True, r["error"]
    assert "Full output saved as block 1" in r["output"]
    assert _CountingSession.calls == 1

    r2 = asyncio.run(ex.execute(
        "print(restore(1, 250, 252))\nprint('--')\nprint(restore(1, grep='row17\\\\b', context=1))",
        session_id="t1",
    ))
    assert r2["success"] is True, r2["error"]
    assert "250 row250\n251 row251\n--" in r2["output"]
    assert "17: 16 row16\n18: 17 row17\n19: 18 row18" in r2["output"]
    assert _CountingSession.calls == 1


def test_restore_unknown_block_names_the_cause(tmp_path):
    ex = _executor(tmp_path, "[]")
    r = asyncio.run(ex.execute("restore(7)", session_id="t1"))
    assert r["success"] is False
    assert "no saved output for block 7" in r["error"]


def test_restore_requires_session_id(tmp_path):
    ex = _executor(tmp_path, "[]")
    r = asyncio.run(ex.execute("restore(1)"))
    assert r["success"] is False
    assert "requires session_id" in r["error"]


def test_truncation_without_session_keeps_old_advice(tmp_path):
    ex = _executor(tmp_path, "[]")
    r = asyncio.run(ex.execute("print('x' * 5000)", max_output_bytes=100))
    assert "LIMIT/TOP" in r["output"]
    assert "out_" not in r["output"]
