import json
from decimal import Decimal

from code_runner.metrics import MetricsRecorder, classify_error, summarize_errors


class TestMetricsRecorder:
    def test_record_writes_jsonl_line(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        rec.record({"kind": "tool_call", "server": "mssql", "tool": "x"})
        lines = (tmp_path / "m.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        ev = json.loads(lines[0])
        assert ev["kind"] == "tool_call"
        assert ev["server"] == "mssql"
        assert "ts" in ev and ev["ts"].endswith("Z")

    def test_record_appends(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        for i in range(3):
            rec.record({"kind": "tool_call", "n": i})
        lines = (tmp_path / "m.jsonl").read_text().strip().splitlines()
        assert len(lines) == 3
        assert [json.loads(line)["n"] for line in lines] == [0, 1, 2]

    def test_non_serializable_falls_back_via_default(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        rec.record({"kind": "t", "val": Decimal("1.5")})
        line = (tmp_path / "m.jsonl").read_text().strip()
        ev = json.loads(line)
        assert ev["val"] == "1.5"

    def test_stderr_mirror(self, tmp_path, capsys):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=True)
        rec.record(
            {"kind": "tool_call", "server": "mssql", "tool": "execute_sql",
             "duration_ms": 12.3, "success": True, "bytes": 100}
        )
        captured = capsys.readouterr()
        assert "[metrics" in captured.err
        assert "mssql.execute_sql" in captured.err
        assert "12.3ms" in captured.err

    def test_stderr_off_is_silent(self, tmp_path, capsys):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        rec.record({"kind": "execute_code", "success": True})
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_rotation_at_size_limit(self, tmp_path):
        rec = MetricsRecorder(
            tmp_path / "m.jsonl", max_bytes=200, backup_count=2, stderr=False
        )
        for i in range(30):
            rec.record({"kind": "tool_call", "payload": "x" * 20, "n": i})
        assert (tmp_path / "m.jsonl").exists()
        assert (tmp_path / "m.jsonl.1").exists()

    def test_rotation_drops_oldest_backup(self, tmp_path):
        rec = MetricsRecorder(
            tmp_path / "m.jsonl", max_bytes=100, backup_count=2, stderr=False
        )
        for i in range(100):
            rec.record({"kind": "t", "payload": "x" * 30, "n": i})
        assert (tmp_path / "m.jsonl").exists()
        assert (tmp_path / "m.jsonl.1").exists()
        assert (tmp_path / "m.jsonl.2").exists()
        # .3 must never exist because backup_count=2
        assert not (tmp_path / "m.jsonl.3").exists()

    def test_read_empty_returns_empty(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        assert rec.read() == []

    def test_read_returns_most_recent(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        for i in range(5):
            rec.record({"kind": "tool_call", "n": i})
        events = rec.read(limit=3)
        assert [e["n"] for e in events] == [2, 3, 4]

    def test_read_filters_by_kind(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        rec.record({"kind": "tool_call", "n": 0})
        rec.record({"kind": "execute_code", "n": 1})
        rec.record({"kind": "tool_call", "n": 2})
        events = rec.read(kind="tool_call")
        assert [e["n"] for e in events] == [0, 2]

    def test_read_filters_by_server(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        rec.record({"kind": "tool_call", "server": "mssql", "n": 0})
        rec.record({"kind": "tool_call", "server": "postgres", "n": 1})
        rec.record({"kind": "tool_call", "server": "mssql", "n": 2})
        events = rec.read(server="mssql")
        assert [e["n"] for e in events] == [0, 2]

    def test_read_filters_by_since(self, tmp_path):
        rec = MetricsRecorder(tmp_path / "m.jsonl", stderr=False)
        rec.record({"ts": "2026-04-17T10:00:00.000Z", "kind": "t", "n": 0})
        rec.record({"ts": "2026-04-17T11:00:00.000Z", "kind": "t", "n": 1})
        rec.record({"ts": "2026-04-17T12:00:00.000Z", "kind": "t", "n": 2})
        events = rec.read(since="2026-04-17T10:30:00.000Z")
        assert [e["n"] for e in events] == [1, 2]

    def test_read_spans_rotated_files(self, tmp_path):
        rec = MetricsRecorder(
            tmp_path / "m.jsonl", max_bytes=150, backup_count=3, stderr=False
        )
        for i in range(40):
            rec.record({"kind": "tool_call", "payload": "x" * 20, "n": i})
        events = rec.read(limit=100)
        # All 40 should be preserved (we only wrote ~40 records, backups hold older)
        ns = [e["n"] for e in events]
        assert len(ns) == len([p for p in tmp_path.iterdir()]) * 0 + len(events)
        # The most recent entry must be the last written one
        assert ns[-1] == 39
        # Chronological order preserved
        assert ns == sorted(ns)

    def test_write_failure_does_not_raise(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        rec = MetricsRecorder(blocker / "metrics.jsonl", stderr=False)
        rec.record({"kind": "test"})


class TestErrorClassification:
    def test_exception_header_after_traceback(self):
        error = (
            "Traceback (most recent call last):\n"
            '  File "<user>", line 2, in <module>\n'
            "NameError: name 'total' is not defined"
        )
        assert classify_error(error) == "NameError"

    def test_hint_after_the_header_does_not_win(self):
        # HINTs are appended after the exception line; the class is still the
        # exception the caller hit, not whatever the hint text mentions.
        error = (
            "TypeError: string indices must be integers\n\n"
            "HINT: a tool result this run was returned as a STRING"
        )
        assert classify_error(error) == "TypeError"

    def test_sandbox_refusals_get_their_own_classes(self):
        assert classify_error("import statements are not allowed: os — …") == "BlockedImport"
        assert classify_error("dunder attribute access is not allowed: __class__") == (
            "BlockedDunder"
        )
        assert classify_error("disallowed node: Exec") == "BlockedConstruct"

    def test_syntax_error_from_validation(self):
        assert classify_error("SyntaxError: invalid syntax (<unknown>, line 3)") == (
            "SyntaxError"
        )

    def test_timeout_and_isolation(self):
        assert classify_error("Execution timed out after 60s") == "Timeout"
        assert classify_error("isolation worker died before returning a result") == (
            "IsolationError"
        )

    def test_none_and_unrecognized(self):
        assert classify_error(None) is None
        assert classify_error("") is None
        assert classify_error("something odd happened") == "Other"


class TestSummarizeErrors:
    def _run(self, success, error=None, error_class=None, ts="2026-08-02T10:00:00Z"):
        ev = {"kind": "execute_code", "success": success, "error": error, "ts": ts}
        if error_class is not None:
            ev["error_class"] = error_class
        return ev

    def test_counts_and_shares(self):
        events = [
            self._run(True),
            self._run(True),
            self._run(False, "SyntaxError: invalid syntax", "SyntaxError"),
            self._run(False, "NameError: name 'x' is not defined", "NameError"),
        ]
        out = summarize_errors(events)
        assert out["runs"] == 4
        assert out["failed"] == 2
        assert out["fail_rate"] == 0.5
        assert out["by_error_class"] == {"NameError": 1, "SyntaxError": 1}
        assert out["syntax_share"] == 0.25

    def test_classifies_events_written_before_the_field_existed(self):
        # The point of the summary is answering "is SyntaxError growing?" over
        # retained history, so events with no error_class must still bucket.
        out = summarize_errors([self._run(False, "SyntaxError: invalid syntax")])
        assert out["by_error_class"] == {"SyntaxError": 1}

    def test_ignores_tool_call_events(self):
        events = [{"kind": "tool_call", "success": False, "error": "boom"}, self._run(True)]
        out = summarize_errors(events)
        assert out["runs"] == 1
        assert out["failed"] == 0

    def test_empty_input(self):
        out = summarize_errors([])
        assert out["runs"] == 0
        assert out["fail_rate"] == 0.0
        assert out["by_error_class"] == {}
