"""SHA-483: HINT с прошлым фиксом похожей ошибки + baseline repeat_error_share.

Индекс строится лениво из metrics.jsonl (7 дней): пара «ошибка (класс + tool) →
следующий успешный execute_code той же session_id», фрагмент фикса лежит в поле
`fix_line` успешного события.
"""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta

import pytest

from code_runner import metrics
from code_runner.executor import CodeExecutor
from code_runner.metrics import MetricsRecorder

ISOLATIONS = [
    "inprocess",
    pytest.param(
        "subprocess",
        marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only"),
    ),
]

MARK = "HINT: в прошлый раз"
VALUE_ERR = "Traceback (most recent call last):\n  File \"<user>\", line 1\nValueError: boom"
KEY_ERR = "Traceback (most recent call last):\n  File \"<user>\", line 1\nKeyError: 'k'"
TOOL_ERR_A = "MCPToolError: srv.alpha returned an error:\nbad"
TOOL_ERR_B = "MCPToolError: srv.beta returned an error:\nbad"
FIX = "rows = int('…')"


def _ts(days_ago: float = 0.0) -> str:
    t = datetime.now(UTC) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _err(sid, error, days_ago=0.0):
    return {
        "ts": _ts(days_ago), "kind": "execute_code", "success": False,
        "session_id": sid, "error": error, "error_class": metrics.classify_error(error),
    }


def _ok(sid, fix=FIX, days_ago=0.0):
    ev = {"ts": _ts(days_ago), "kind": "execute_code", "success": True,
          "session_id": sid, "error": None, "error_class": None}
    if fix is not None:
        ev["fix_line"] = fix
    return ev


def _write(path, events):
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
                    encoding="utf-8")


def _index(tmp_path, events):
    path = tmp_path / "m.jsonl"
    if events is not None:
        _write(path, events)
    return metrics.PastFixIndex(MetricsRecorder(path, stderr=False))


class _EmptyPool:
    sessions: dict = {}
    tools: dict = {}


def _run(isolation, recorder, *blocks, sid="s-new"):
    executor = CodeExecutor(pool=_EmptyPool(), isolation=isolation, recorder=recorder)

    async def scenario():
        return [await executor.execute(code, session_id=sid) for code in blocks]

    return asyncio.run(scenario())


# --- 3. позитивный кейс -------------------------------------------------------


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_repeat_error_gets_one_past_fix_hint(tmp_path, isolation):
    path = tmp_path / "m.jsonl"
    _write(path, [_err("old", VALUE_ERR, 1), _ok("old", days_ago=1)])
    rec = MetricsRecorder(path, stderr=False)
    (res,) = _run(isolation, rec, "raise ValueError('boom')")
    assert not res["success"]
    assert res["error"].count(MARK) == 1
    line = next(ln for ln in res["error"].splitlines() if ln.startswith(MARK))
    assert len(line) <= 300
    assert "ValueError" in line
    assert FIX in line


def test_tool_error_key_includes_tool(tmp_path):
    idx = _index(tmp_path, [_err("a", TOOL_ERR_A, 1), _ok("a", days_ago=1)])
    hint = idx.hint(TOOL_ERR_A)
    assert hint.count(MARK) == 1
    assert "srv.alpha" in hint
    assert FIX in hint
    assert len(hint.strip()) <= 300


def test_same_class_other_message_gets_no_hint(tmp_path):
    """Без tool ключ — класс + сообщение: чужой TypeError не даёт своего фикса."""
    other = "Traceback (most recent call last):\nTypeError: 'NoneType' object is not iterable"
    mine = "Traceback (most recent call last):\nTypeError: 'dict' object can't be awaited"
    idx = _index(tmp_path, [_err("a", other, 1), _ok("a", days_ago=1)])
    assert idx.hint(mine) == ""


def test_message_key_ignores_digits(tmp_path):
    """Номер строки в сообщении не делит одну ошибку на две."""
    first = "Traceback (most recent call last):\nIndexError: index 3 out of range"
    again = "Traceback (most recent call last):\nIndexError: index 17 out of range"
    idx = _index(tmp_path, [_err("a", first, 1), _ok("a", days_ago=1)])
    assert FIX in idx.hint(again)


def test_long_fragment_keeps_hint_within_300(tmp_path):
    idx = _index(tmp_path, [_err("a", VALUE_ERR, 1), _ok("a", fix="x" * 300, days_ago=1)])
    assert len(idx.hint(VALUE_ERR).strip()) <= 300


# --- 4. негативные кейсы ------------------------------------------------------

NEGATIVE = {
    "no_pair": [_ok("a")],
    "error_without_success": [_err("a", VALUE_ERR, 1)],
    "other_class": [_err("a", KEY_ERR, 1), _ok("a", days_ago=1)],
    "success_other_session": [_err("a", VALUE_ERR, 1), _ok("b", days_ago=1)],
    "older_than_7_days": [_err("a", VALUE_ERR, 10), _ok("a", days_ago=10)],
    "success_without_fix_line": [_err("a", VALUE_ERR, 1), _ok("a", fix=None, days_ago=1)],
}


@pytest.mark.parametrize("name", list(NEGATIVE))
def test_no_hint(tmp_path, name):
    assert _index(tmp_path, NEGATIVE[name]).hint(VALUE_ERR) == ""


def test_same_class_other_tool_no_hint(tmp_path):
    idx = _index(tmp_path, [_err("a", TOOL_ERR_A, 1), _ok("a", days_ago=1)])
    assert idx.hint(TOOL_ERR_B) == ""


def test_metrics_disabled_no_hint_no_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_RUNNER_METRICS", "0")
    assert metrics.recorder_from_env() is None
    (res,) = _run("inprocess", metrics.recorder_from_env(), "raise ValueError('boom')")
    assert "ValueError" in res["error"]
    assert MARK not in res["error"]


def test_missing_file_no_hint(tmp_path):
    assert _index(tmp_path, None).hint(VALUE_ERR) == ""


def test_empty_file_no_hint(tmp_path):
    path = tmp_path / "m.jsonl"
    path.write_text("")
    assert metrics.PastFixIndex(MetricsRecorder(path, stderr=False)).hint(VALUE_ERR) == ""


def test_broken_lines_no_hint_no_error(tmp_path):
    path = tmp_path / "m.jsonl"
    path.write_text('garbage\n{"a":\n[1]\n"str"\n42\n\n', encoding="utf-8")
    assert metrics.PastFixIndex(MetricsRecorder(path, stderr=False)).hint(VALUE_ERR) == ""


def test_broken_lines_do_not_hide_valid_pair(tmp_path):
    path = tmp_path / "m.jsonl"
    _write(path, [_err("a", VALUE_ERR, 1), _ok("a", days_ago=1)])
    with path.open("a") as f:
        f.write("garbage\n[1]\n")
    idx = metrics.PastFixIndex(MetricsRecorder(path, stderr=False))
    assert idx.hint(VALUE_ERR).count(MARK) == 1


# --- 5. приватность и размер --------------------------------------------------


@pytest.mark.parametrize("isolation", ISOLATIONS)
def test_fix_line_is_short_and_has_no_secret(tmp_path, isolation):
    secret = "SECRET-abc-123456"
    path = tmp_path / "m.jsonl"
    rec = MetricsRecorder(path, stderr=False)
    long_line = "total = " + " + ".join(["1"] * 400)
    first, fixed = _run(
        isolation, rec,
        "raise ValueError('boom')",
        f"token = '{secret}'\n{long_line}",
        sid="priv",
    )
    assert not first["success"] and fixed["success"], fixed["error"]
    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    events = [json.loads(ln) for ln in raw.splitlines()]
    fix = [e["fix_line"] for e in events if e.get("fix_line")]
    assert len(fix) == 1 and 0 < len(fix[0]) <= 300
    (res,) = _run(isolation, rec, "raise ValueError('boom')", sid="priv-2")
    assert secret not in res["error"]
    assert res["error"].count(MARK) == 1


def test_old_rows_without_new_field_do_not_break_summaries(tmp_path):
    events = [_err("a", VALUE_ERR), _ok("a", fix=None)]
    s = metrics.summarize_errors(events)
    assert s["failed"] == 1
    assert metrics.summarize_skills(events, {}) is not None
    assert _index(tmp_path, events).hint(VALUE_ERR) == ""


# --- 6. ленивость и кэш -------------------------------------------------------


def test_index_is_lazy_and_cached_by_mtime_size(tmp_path, monkeypatch):
    path = tmp_path / "m.jsonl"
    _write(path, [_err("a", VALUE_ERR, 1), _ok("a", days_ago=1)])
    reads = []
    real = MetricsRecorder.read

    def counting(self, *a, **kw):
        reads.append(1)
        return real(self, *a, **kw)

    monkeypatch.setattr(MetricsRecorder, "read", counting)
    idx = metrics.PastFixIndex(MetricsRecorder(path, stderr=False))
    assert reads == []
    assert idx.hint(None) == "" and idx.hint("") == ""
    assert reads == []
    assert idx.hint(VALUE_ERR)
    assert idx.hint(VALUE_ERR)
    assert len(reads) == 1
    with path.open("a") as f:
        f.write(json.dumps(_ok("zz")) + "\n")
    assert idx.hint(VALUE_ERR)
    assert len(reads) == 2


# --- 7. baseline --------------------------------------------------------------


def test_repeat_error_share_known_numbers():
    events = [
        _err("s1", VALUE_ERR), _err("s1", VALUE_ERR), _err("s1", KEY_ERR),
        _err("s2", VALUE_ERR), _ok("s1"),
    ]
    s = metrics.summarize_errors(events)
    assert s["failed"] == 4
    assert s["repeat_errors"] == 1
    assert s["repeat_error_share"] == 0.25


def test_repeat_error_share_empty_window():
    assert metrics.summarize_errors([])["repeat_error_share"] == 0.0
    assert metrics.summarize_errors([_ok("a")])["repeat_error_share"] == 0.0
