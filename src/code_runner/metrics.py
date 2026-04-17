"""
Metrics recording for code-runner.

Writes JSONL events to a log file with size-based rotation, and optionally
mirrors a short line to stderr for live visibility. Reading supports simple
filtering (time/server/kind) so `get_metrics` MCP tool can surface recent
activity without pulling the whole history.
"""

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
DEFAULT_BACKUP_COUNT = 3


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with millisecond precision."""
    now = datetime.now(timezone.utc)
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
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

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
                try:
                    print(self._format_short(event), file=sys.stderr)
                except Exception:
                    pass

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
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return up to `limit` most recent events matching the filters.

        since: ISO-8601 string — keep only events with ts >= since.
        server: match event["server"] exactly.
        kind: match event["kind"] exactly.
        """
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
                        if since and ev.get("ts", "") < since:
                            continue
                        if server and ev.get("server") != server:
                            continue
                        if kind and ev.get("kind") != kind:
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
