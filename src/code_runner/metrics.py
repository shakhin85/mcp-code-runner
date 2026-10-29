"""
Metrics recording for code-runner.

Writes JSONL events to a log file with size-based rotation, and optionally
mirrors a short line to stderr for live visibility. Reading supports simple
filtering (time/server/kind) so `get_metrics` MCP tool can surface recent
activity without pulling the whole history.
"""

import contextlib
import hashlib
import json
import os
import re
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
DEFAULT_BACKUP_COUNT = 3

# An exception header line: "ValueError: boom", "MCPToolError: ...". Hints are
# appended after the header, and a traceback precedes it, so we scan every line
# and keep the last match — that is the exception the user actually hit.
_EXC_HEADER_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Interrupt|Exit|Warning)): "
)

# Sandbox refusals never reach an exception class of their own — validate_code
# raises a plain ValueError whose message is the whole signal. Mapping them to
# named classes keeps "the sandbox said no" separable from "the code broke".
_REFUSAL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("import statements are not allowed", "BlockedImport"),
    ("dunder attribute access is not allowed", "BlockedDunder"),
    ("disallowed node", "BlockedConstruct"),
    ("disallowed name", "BlockedConstruct"),
    ("disallowed attribute root", "BlockedConstruct"),
)


def classify_error(error: str | None) -> str | None:
    """Bucket an execute_code error string into a stable class name.

    Recorded per event going forward, and applied on read to events written
    before the field existed — so a question like "is the SyntaxError share
    growing?" can be answered over the whole retained history, not just since
    the last deploy.
    """
    if not error:
        return None
    text = error.strip()
    if not text:
        return None
    low = text.lower()
    for prefix, name in _REFUSAL_PREFIXES:
        if low.startswith(prefix):
            return name
    if low.startswith("execution timed out") or "timed out after" in low:
        return "Timeout"
    if low.startswith("isolation error") or low.startswith("isolation worker died"):
        return "IsolationError"
    found = None
    for line in text.splitlines():
        m = _EXC_HEADER_RE.match(line.strip())
        if m:
            found = m.group(1)
    if found:
        return found
    return "Other"


# SHA-483: HINT с прошлым фиксом. Ключ ошибки — класс + tool (если ошибка пришла
# из MCP-вызова) либо класс + сообщение исключения; фрагмент фикса — поле fix_line
# следующего успешного блока сессии.
PAST_FIX_WINDOW_DAYS = 7
PAST_FIX_MAX_CHARS = 300
_TOOL_RE = re.compile(r"\b([A-Za-z_]\w*\.[A-Za-z_]\w*) returned an error:")
_STRING_LITERAL_RE = re.compile(r"""(["'])(?:\\.|(?!\1).)*(?:\1|$)""")
_DIGITS_RE = re.compile(r"\d+")
_MESSAGE_KEY_MAX = 200


def _message_key(text: str) -> str:
    """Строка исключения (последний заголовок `XxxError: …`, иначе первая строка),
    цифры заменены на 0: номера строк и счётчики не делят одну ошибку на несколько."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    headers = [ln for ln in lines if _EXC_HEADER_RE.match(ln)]
    line = headers[-1] if headers else (lines[0] if lines else "")
    return _DIGITS_RE.sub("0", line)[:_MESSAGE_KEY_MAX]


def error_key(error: str | None) -> tuple[str, str, str] | None:
    """(класс, tool, сообщение) ошибки.

    Ошибка MCP-вызова различается по tool, сообщение пусто. Ошибка без tool
    различается по сообщению: только класс (`TypeError`) сводит в один ключ
    несвязанные ошибки, и HINT подсовывает чужой фикс.
    """
    cls = classify_error(error)
    if cls is None:
        return None
    m = _TOOL_RE.search(error or "")
    if m:
        return (cls, m.group(1), "")
    return (cls, "", _message_key(error or ""))


def fix_fragment(failed_code: str, fixed_code: str) -> str:
    """Первая строка fixed_code, которой не было в failed_code, до 300 символов.

    Строковые литералы заменены на '…': в metrics.jsonl не должны попадать
    значения аргументов tool (токены, SQL, ключи).
    """
    old = {ln.strip() for ln in failed_code.splitlines()}
    lines = [ln.strip() for ln in fixed_code.splitlines() if ln.strip()]
    line = next((ln for ln in lines if ln not in old), lines[0] if lines else "")
    line = _STRING_LITERAL_RE.sub(lambda m: m.group(1) + "…" + m.group(1), line)
    return line[:PAST_FIX_MAX_CHARS]


class PastFixIndex:
    """Ленивый индекс «ключ ошибки → как исправили в прошлый раз».

    Файл не читается при создании и пока не пришла ошибка; кэш живёт, пока не
    меняются mtime и размер основного файла метрик.
    """

    def __init__(self, recorder: "MetricsRecorder", days: int = PAST_FIX_WINDOW_DAYS) -> None:
        self._recorder = recorder
        self._days = days
        self._sig: tuple[int, int] | None = None
        self._fixes: dict[tuple[str, str, str], str] = {}

    def _load(self) -> dict[tuple[str, str, str], str]:
        try:
            st = self._recorder.path.stat()
        except OSError:
            return {}
        sig = (st.st_mtime_ns, st.st_size)
        if sig != self._sig:
            self._fixes = self._build()
            self._sig = sig
        return self._fixes

    def _build(self) -> dict[tuple[str, str, str], str]:
        since = (datetime.now(UTC) - timedelta(days=self._days)).strftime("%Y-%m-%dT%H:%M:%S")
        pending: dict[str, set[tuple[str, str, str]]] = {}
        fixes: dict[tuple[str, str, str], str] = {}
        for ev in self._recorder.read(since=since, limit=0):
            sid = ev.get("session_id")
            if ev.get("kind") != "execute_code" or not sid:
                continue
            if ev.get("success") is False:
                key = error_key(ev.get("error"))
                if key:
                    pending.setdefault(sid, set()).add(key)
            elif ev.get("success") is True:
                frag = ev.get("fix_line")
                for key in pending.pop(sid, ()):
                    if isinstance(frag, str) and frag:
                        fixes[key] = frag
        return fixes

    def hint(self, error: str | None) -> str:
        """Одна строка HINT (с ведущим переводом строки) или пустая строка."""
        key = error_key(error)
        if key is None:
            return ""
        try:
            frag = self._load().get(key)
        except Exception:
            return ""
        if not frag:
            return ""
        cls, tool, _ = key
        head = f"HINT: в прошлый раз {cls}{f' ({tool})' if tool else ''} исправили так: "
        return "\n\n" + (head + frag)[:PAST_FIX_MAX_CHARS]


def summarize_errors(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate execute_code events into a failure breakdown.

    ``syntax_share`` is the routing signal behind this: writing code with
    escaping is harder than issuing one direct tool call, so a rising share of
    SyntaxError means the caller is being pushed into the sandbox for tasks too
    small to earn it. The other classes are the error-UX backlog — a class that
    grows is a missing HINT.
    """
    runs = [e for e in events if e.get("kind") == "execute_code"]
    failures = [e for e in runs if e.get("success") is False]
    by_class: dict[str, int] = {}
    for ev in failures:
        cls = ev.get("error_class") or classify_error(ev.get("error")) or "Other"
        by_class[cls] = by_class.get(cls, 0) + 1
    seen: set[tuple[str, tuple[str, str, str]]] = set()
    repeats = 0
    for ev in failures:
        sid = ev.get("session_id")
        if not sid:
            continue
        cls = ev.get("error_class") or classify_error(ev.get("error")) or "Other"
        mark = (sid, error_key(ev.get("error")) or (cls, "", ""))
        if mark in seen:
            repeats += 1
        seen.add(mark)
    total = len(runs)
    ordered = dict(sorted(by_class.items(), key=lambda kv: (-kv[1], kv[0])))
    return {
        "runs": total,
        "failed": len(failures),
        "fail_rate": round(len(failures) / total, 4) if total else 0.0,
        "by_error_class": ordered,
        "repeat_errors": repeats,
        "repeat_error_share": round(repeats / len(failures), 4) if failures else 0.0,
        "syntax_errors": by_class.get("SyntaxError", 0),
        "syntax_share": (
            round(by_class.get("SyntaxError", 0) / total, 4) if total else 0.0
        ),
        "window": {
            "first_ts": runs[0].get("ts") if runs else None,
            "last_ts": runs[-1].get("ts") if runs else None,
        },
    }


# Trim-report thresholds (SHA-484; TroVE, arXiv 2401.12869: grow + trim).
# A skill function is a removal candidate when it had no calls in the window,
# or failed in at least SKILL_TRIM_ERROR_SHARE of at least SKILL_TRIM_MIN_CALLS
# calls — fewer calls than that say nothing about the error share.
SKILL_TRIM_WINDOW_DAYS = 30
SKILL_TRIM_MIN_CALLS = 5
SKILL_TRIM_ERROR_SHARE = 0.3


def summarize_fallbacks(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Share of failed skill calls the session recovered from via a raw tool (SHA-523).

    A fatal `skill_call` (it ended the block) carrying `fallback` (raw tools
    from the skill's `replaces:`) is recovered when the session's next execute_code block
    called one of those tools successfully (`ok_tools`). Events are in file
    order: a block's skill_call lines follow its own execute_code line, so the
    next execute_code of the session is the next block.
    """
    pending: dict[str, set[str]] = {}
    failures = recovered = 0
    for ev in events:
        sid = ev.get("session_id")
        if not sid:
            continue
        kind = ev.get("kind")
        if kind == "skill_call" and ev.get("fatal") and ev.get("fallback"):
            pending.setdefault(sid, set()).update(ev["fallback"])
        elif kind == "execute_code" and sid in pending:
            failures += 1
            recovered += bool(pending.pop(sid) & set(ev.get("ok_tools") or ()))
    return {
        "failures": failures,
        "recovered": recovered,
        "share": round(recovered / failures, 4) if failures else 0.0,
    }


def summarize_skills(
    events: list[dict[str, Any]], installed: dict[str, list[str]]
) -> dict[str, Any]:
    """Per `skills.<name>.<fn>` usage from skill_call events, with trim candidates.

    `installed` maps skill name → its public functions, so a function never
    called in the window still gets a row (calls=0, candidate "unused").
    Called functions of a skill no longer installed keep their rows too.
    Removal itself is a human decision; this only names the candidates.
    """
    calls = [e for e in events if e.get("kind") == "skill_call"]
    rows: dict[tuple[str, str], dict[str, Any]] = {
        (skill, fn): {"skill": skill, "fn": fn, "calls": 0, "errors": 0, "last_call": None}
        for skill, fns in installed.items()
        for fn in fns
    }
    for ev in calls:
        key = (ev.get("skill"), ev.get("fn"))
        row = rows.setdefault(
            key, {"skill": key[0], "fn": key[1], "calls": 0, "errors": 0, "last_call": None}
        )
        row["calls"] += 1
        row["errors"] += ev.get("success") is False
        row["last_call"] = max(row["last_call"] or "", ev.get("ts") or "") or None
    out = []
    for row in rows.values():
        share = row["errors"] / row["calls"] if row["calls"] else 0.0
        if row["calls"] == 0:
            candidate = "unused"
        elif row["calls"] >= SKILL_TRIM_MIN_CALLS and share >= SKILL_TRIM_ERROR_SHARE:
            candidate = "failing"
        else:
            candidate = None
        out.append({**row, "error_pct": round(share * 100, 1), "candidate": candidate})
    out.sort(key=lambda r: (r["candidate"] is None, -r["calls"], r["skill"], r["fn"]))
    return {
        "thresholds": {
            "window_days": SKILL_TRIM_WINDOW_DAYS,
            "min_calls": SKILL_TRIM_MIN_CALLS,
            "error_share": SKILL_TRIM_ERROR_SHARE,
        },
        # Rotation can keep less history than the window: this is what the
        # numbers actually cover.
        "covered_from": calls[0].get("ts") if calls else None,
        "candidates": sum(r["candidate"] is not None for r in out),
        "fallback": summarize_fallbacks(events),
        "rows": out,
    }


def code_fingerprint(code: str) -> str:
    """Whitespace-normalized sha256[:16] of a code snippet.

    Same normalization as the external skill-distiller (strip lines, drop
    blanks), so repeated snippets cluster across both systems without the
    metrics log having to store the code text itself.
    """
    lines = [ln.strip() for ln in code.strip().splitlines()]
    normalized = "\n".join(ln for ln in lines if ln)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with millisecond precision."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class MetricsRecorder:
    """Append-only JSONL recorder with size-based rotation.

    Thread-safe via an internal lock. Failures writing or rotating are
    swallowed — metrics must never break the hot path — but the failure is
    printed once to stderr so operators can notice.
    """

    def __init__(
        self,
        path: Path | str,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
        stderr: bool = True,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.stderr = stderr
        self._lock = threading.Lock()
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def _rotated_path(self, i: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{i}")

    def _rotate_if_needed(self, incoming: int) -> None:
        if not self.path.exists():
            return
        if self.path.stat().st_size + incoming <= self.max_bytes:
            return
        oldest = self._rotated_path(self.backup_count)
        if oldest.exists():
            oldest.unlink()
        for i in range(self.backup_count - 1, 0, -1):
            src = self._rotated_path(i)
            if src.exists():
                src.rename(self._rotated_path(i + 1))
        self.path.rename(self._rotated_path(1))

    def _format_short(self, event: dict) -> str:
        kind = event.get("kind", "?")
        ts = event.get("ts", "")
        # extract HH:MM:SS.mmm from ISO ts
        time_part = ts.split("T", 1)[1][:12] if "T" in ts else ts
        parts = [f"[metrics {time_part}]", kind]
        if kind == "tool_call":
            parts.append(f"{event.get('server')}.{event.get('tool')}")
        dur = event.get("duration_ms")
        if dur is not None:
            parts.append(f"{dur:.1f}ms")
        bts = event.get("bytes")
        if bts is not None:
            parts.append(f"{bts}B")
        if event.get("success") is False:
            err = str(event.get("error") or "")[:60]
            parts.append(f"ERR: {err}")
        return " ".join(parts)

    def record(self, event: dict) -> None:
        """Append an event; mutates the dict to add a ts field if missing."""
        if "ts" not in event:
            event = {"ts": _utc_now_iso(), **event}
        try:
            line = json.dumps(event, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return
        with self._lock:
            try:
                self._rotate_if_needed(len(line) + 1)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:
                print(f"[metrics] write failed: {e}", file=sys.stderr)
                return
            if self.stderr:
                with contextlib.suppress(Exception):
                    print(self._format_short(event), file=sys.stderr)

    def _iter_files_chronological(self) -> list[Path]:
        files: list[Path] = []
        for i in range(self.backup_count, 0, -1):
            p = self._rotated_path(i)
            if p.exists():
                files.append(p)
        if self.path.exists():
            files.append(self.path)
        return files

    def read(
        self,
        since: str | None = None,
        server: str | None = None,
        kind: str | tuple[str, ...] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return up to `limit` most recent events matching the filters.

        since: ISO-8601 string — keep only events with ts >= since.
        server: match event["server"] exactly.
        kind: match event["kind"] exactly (a tuple matches any of its members);
            an empty string means no filter, as None does.
        """
        kinds = tuple(k for k in ((kind,) if isinstance(kind, str) else kind or ()) if k)
        events: list[dict[str, Any]] = []
        for p in self._iter_files_chronological():
            try:
                with p.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(ev, dict):
                            continue
                        if since and ev.get("ts", "") < since:
                            continue
                        if server and ev.get("server") != server:
                            continue
                        if kinds and ev.get("kind") not in kinds:
                            continue
                        events.append(ev)
            except OSError:
                continue
        if limit > 0 and len(events) > limit:
            events = events[-limit:]
        return events


def recorder_from_env() -> MetricsRecorder | None:
    """Build a recorder from CODE_RUNNER_METRICS_* env vars, or return None.

    CODE_RUNNER_METRICS=0 disables entirely.
    CODE_RUNNER_METRICS_PATH overrides the default path.
    CODE_RUNNER_METRICS_STDERR=0 suppresses the short stderr line.
    """
    if os.environ.get("CODE_RUNNER_METRICS", "1") == "0":
        return None
    default_path = Path.home() / ".cache" / "code-runner" / "metrics.jsonl"
    path = os.environ.get("CODE_RUNNER_METRICS_PATH") or str(default_path)
    stderr = os.environ.get("CODE_RUNNER_METRICS_STDERR", "1") != "0"
    return MetricsRecorder(path, stderr=stderr)
