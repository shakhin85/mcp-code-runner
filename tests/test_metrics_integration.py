import asyncio

from code_runner.executor import CodeExecutor, _ToolNamespace
from code_runner.metrics import MetricsRecorder


class _FakeText:
    def __init__(self, text):
        self.text = text


class _FakeResult:
    def __init__(self, text):
        self.content = [_FakeText(text)]


class _FakeSession:
    def __init__(self, payload_text="ok", raise_on_call=None):
        self._payload = payload_text
        self._raise = raise_on_call
        self.last_call = None

    async def call_tool(self, name, kwargs):
        self.last_call = (name, kwargs)
        if self._raise is not None:
            raise self._raise
        return _FakeResult(self._payload)


class _FakeTool:
    def __init__(self, name, description=""):
        self.name = name
        self.description = description


class _FakePool:
    def __init__(self, sessions=None, tools=None):
        self.sessions = sessions or {}
        self.tools = tools or {}


class TestToolCallMetrics:
    def test_tool_call_event_recorded(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        session = _FakeSession('{"rows": [1, 2]}')
        stats = {"tool_calls": 0, "auto_limit_hits": 0}
        ns = _ToolNamespace(
            "mssql", session, [_FakeTool("execute_sql")],
            auto_limit=0, stats=stats, recorder=rec,
        )
        asyncio.run(ns.execute_sql(query="SELECT 1"))

        events = rec.read(kind="tool_call")
        assert len(events) == 1
        ev = events[0]
        assert ev["server"] == "mssql"
        assert ev["tool"] == "execute_sql"
        assert ev["success"] is True
        assert ev["bytes"] == len('{"rows": [1, 2]}'.encode())
        assert ev["duration_ms"] >= 0
        assert stats["tool_calls"] == 1

    def test_tool_call_error_recorded(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        session = _FakeSession(raise_on_call=RuntimeError("boom"))
        stats = {"tool_calls": 0, "auto_limit_hits": 0}
        ns = _ToolNamespace(
            "mssql", session, [_FakeTool("execute_sql")],
            auto_limit=0, stats=stats, recorder=rec,
        )
        try:
            asyncio.run(ns.execute_sql(query="SELECT 1"))
        except RuntimeError:
            pass

        events = rec.read(kind="tool_call")
        assert len(events) == 1
        ev = events[0]
        assert ev["success"] is False
        assert "boom" in ev["error"]
        assert stats["tool_calls"] == 1

    def test_limit_applied_counted(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        session = _FakeSession("[]")
        stats = {"tool_calls": 0, "auto_limit_hits": 0}
        ns = _ToolNamespace(
            "mssql", session, [_FakeTool("execute_sql")],
            auto_limit=500, stats=stats, recorder=rec,
        )
        asyncio.run(ns.execute_sql(query="SELECT * FROM t"))

        assert stats["auto_limit_hits"] == 1
        events = rec.read(kind="tool_call")
        assert events[0]["limit_applied"] is True

    def test_limit_not_applied_for_user_limit(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        session = _FakeSession("[]")
        stats = {"tool_calls": 0, "auto_limit_hits": 0}
        ns = _ToolNamespace(
            "mssql", session, [_FakeTool("execute_sql")],
            auto_limit=500, stats=stats, recorder=rec,
        )
        asyncio.run(ns.execute_sql(query="SELECT TOP 10 * FROM t"))

        assert stats["auto_limit_hits"] == 0


class TestExecuteCodeRollup:
    def test_success_rollup(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        executor = CodeExecutor(_FakePool(), recorder=rec)
        result = asyncio.run(executor.execute("print('hi')"))
        assert result["success"] is True

        events = rec.read(kind="execute_code")
        assert len(events) == 1
        ev = events[0]
        assert ev["success"] is True
        assert ev["tool_calls"] == 0
        assert ev["auto_limit_hits"] == 0
        assert ev["output_bytes_raw"] > 0
        assert ev["truncated"] is False

    def test_error_path_still_rolls_up(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        executor = CodeExecutor(_FakePool(), recorder=rec)
        asyncio.run(executor.execute("raise ValueError('x')"))

        events = rec.read(kind="execute_code")
        assert len(events) == 1
        assert events[0]["success"] is False
        assert "ValueError" in events[0]["error"]

    def test_validation_failure_rolls_up(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        executor = CodeExecutor(_FakePool(), recorder=rec)
        asyncio.run(executor.execute("import os"))

        events = rec.read(kind="execute_code")
        assert len(events) == 1
        assert events[0]["success"] is False
        assert "import" in events[0]["error"]

    def test_truncation_flag_set(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        executor = CodeExecutor(_FakePool(), recorder=rec)
        asyncio.run(executor.execute(
            "print('x' * 5000)", max_output_bytes=100,
        ))

        events = rec.read(kind="execute_code")
        assert events[0]["truncated"] is True
        assert events[0]["output_bytes_raw"] > events[0]["output_bytes_sent"]

    def test_session_id_in_rollup(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        executor = CodeExecutor(_FakePool(), recorder=rec)
        asyncio.run(executor.execute("x = 1", session_id="s1"))

        events = rec.read(kind="execute_code")
        assert events[0]["session_id"] == "s1"

    def test_tool_call_count_aggregated(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        session = _FakeSession('{"ok": true}')
        pool = _FakePool(
            sessions={"mssql": session},
            tools={"mssql": [_FakeTool("execute_sql")]},
        )
        executor = CodeExecutor(pool, recorder=rec)
        code = (
            "r1 = await mssql.execute_sql(query='SELECT 1')\n"
            "r2 = await mssql.execute_sql(query='SELECT 2')\n"
            "print('done')"
        )
        result = asyncio.run(executor.execute(code))
        assert result["success"] is True

        rollups = rec.read(kind="execute_code")
        assert rollups[0]["tool_calls"] == 2
        tool_calls = rec.read(kind="tool_call")
        assert len(tool_calls) == 2


class TestRecorderNoneIsNoOp:
    def test_executor_without_recorder_works(self):
        executor = CodeExecutor(_FakePool(), recorder=None)
        result = asyncio.run(executor.execute("print('ok')"))
        assert result["success"] is True

    def test_namespace_without_recorder_works(self):
        session = _FakeSession("[]")
        stats = {"tool_calls": 0, "auto_limit_hits": 0}
        ns = _ToolNamespace(
            "mssql", session, [_FakeTool("execute_sql")],
            auto_limit=0, stats=stats, recorder=None,
        )
        asyncio.run(ns.execute_sql(query="SELECT 1"))
        assert stats["tool_calls"] == 1
